"""Async-path AI enrichment: one Claude Haiku call per micro-batch.

Failures never affect the critical path: the rule-based score already exists,
so on failure the batch simply comes back unenriched (graceful degradation).
"""

import json
import logging
import time

import anthropic

from src.models import Enrichment
from src.resilience import CircuitBreaker, backoff_delay

logger = logging.getLogger(__name__)

ENRICHMENT_PROMPT = """You are a fraud analyst for a fintech payment platform.

For EACH event below, assess account-takeover / fraud risk using the event and
the user's context. Return ONLY a JSON array with one object per event:
- event_id: copied exactly from the input
- risk_score: integer 0-100
- anomaly_type: one of [account_takeover, card_testing, velocity_abuse, geo_anomaly, amount_anomaly, none]
- confidence: float 0.0-1.0
- reasoning: one short sentence

No markdown, no text outside the JSON array.

Events:
{events_json}"""


class EnrichmentUnavailable(Exception):
    """Raised when the batch could not be enriched (breaker open or retries exhausted)."""


class AIEnricher:
    def __init__(self, client, model: str, max_tokens: int = 1500, max_retries: int = 3,
                 breaker: CircuitBreaker = None, sleep=time.sleep, cost_guard=None, metrics=None):
        self.client = client  # anthropic.Anthropic (or mock)
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.breaker = breaker or CircuitBreaker()
        self.sleep = sleep
        self.cost_guard = cost_guard
        self.metrics = metrics
        self.calls = 0
        self.failures = 0

    def enrich_batch(self, items: list) -> dict:
        """items: list of (Event, context dict). Returns {event_id: Enrichment}.

        Events missing/invalid in the response are simply absent from the result.
        Raises EnrichmentUnavailable if the call itself cannot succeed.
        """
        if not items:
            return {}
        if not self.breaker.allow():
            if self.metrics:
                self.metrics.incr("breaker_short_circuits")
            raise EnrichmentUnavailable("circuit breaker open")

        payload = [
            {"event_id": e.event_id, "user_id": e.user_id, "amount": e.amount,
             "merchant": e.merchant, "merchant_category": e.merchant_category,
             "country": e.country, "context": ctx}
            for e, ctx in items
        ]
        prompt = ENRICHMENT_PROMPT.format(events_json=json.dumps(payload, default=str))

        last_err = None
        for attempt in range(self.max_retries):
            try:
                self.calls += 1
                t0 = time.perf_counter()
                resp = self.client.messages.create(
                    model=self.model, max_tokens=self.max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                if self.metrics:
                    self.metrics.observe("api_latency_ms", (time.perf_counter() - t0) * 1000)
                if self.cost_guard:
                    self.cost_guard.record_usage(resp.usage.input_tokens, resp.usage.output_tokens)
                result = self._parse(resp.content[0].text, {e.event_id for e, _ in items})
                self.breaker.record_success()
                return result
            except (anthropic.RateLimitError, anthropic.APIError) as e:
                last_err = e
                self.failures += 1
                if self.metrics:
                    self.metrics.incr("api_failures")
                logger.warning("enrichment attempt %d/%d failed: %s", attempt + 1, self.max_retries, e)
            except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as e:
                last_err = e
                self.failures += 1
                if self.metrics:
                    self.metrics.incr("api_failures")
                logger.warning("unparseable enrichment response: %s", e)
            # Article: "if three API calls fail in a row, stop sending requests for sixty
            # seconds" -> every failed call counts, not every failed batch.
            self.breaker.record_failure()
            if not self.breaker.allow():
                if self.metrics:
                    self.metrics.incr("breaker_short_circuits")
                raise EnrichmentUnavailable(f"circuit breaker open after: {last_err}")
            if attempt < self.max_retries - 1:
                self.sleep(backoff_delay(attempt))
        raise EnrichmentUnavailable(f"enrichment failed: {last_err}")

    @staticmethod
    def _parse(text: str, expected_ids: set) -> dict:
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array")
        out = {}
        for item in parsed:
            try:
                enr = Enrichment(**item)
            except Exception:
                continue  # one bad item must not poison the batch
            if enr.event_id in expected_ids:
                out[enr.event_id] = enr
        return out
