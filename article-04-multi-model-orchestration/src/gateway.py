"""ModelGateway: the middleware stack around every model call.

    cache -> circuit breaker -> Anthropic call (timeout + retries) -> contract validation
          -> cost tracking -> cache write

Failures never raise: they become explicit fallback results (fallback=True, confidence 0.0,
failure_reason set) so downstream tables can filter them into a _fallback table.
"""

import json
import logging
import threading
import time
from typing import Callable, Optional

import anthropic

from .cache import ReviewCache
from .circuit_breaker import OPEN, BreakerRegistry
from .config import RouterConfig
from .cost_tracker import CallRecord, CostTracker, utc_now_iso
from .prompts import PROMPTS
from .queues import InMemoryQueue
from .router import AspectRetriever, KeywordAspectRetriever
from .schemas import (
    ContractViolation, TaskResult, fallback_payload, validate_review_input, validate_task_output,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 300


class ModelGateway:
    def __init__(
        self,
        client,
        config: RouterConfig,
        cache: Optional[ReviewCache] = None,
        cost_tracker: Optional[CostTracker] = None,
        breakers: Optional[BreakerRegistry] = None,
        retriever: Optional[AspectRetriever] = None,
        retry_queue: Optional[InMemoryQueue] = None,
        dead_letter_queue: Optional[InMemoryQueue] = None,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.config = config
        self.cache = cache
        self.cost = cost_tracker if cost_tracker is not None else CostTracker()
        self.breakers = breakers or BreakerRegistry(config.breaker)
        self.retriever = retriever or KeywordAspectRetriever()
        self.retry_queue = retry_queue if retry_queue is not None else InMemoryQueue("retry")
        self.dlq = dead_letter_queue if dead_letter_queue is not None else InMemoryQueue("dlq")
        self._clock = clock
        self._sleep = sleep
        self.contract_violations = 0
        self._lock = threading.Lock()

    def record_contract_violation(self) -> None:
        with self._lock:  # gateway is shared by fan-out / Dagster worker threads
            self.contract_violations += 1

    # ---- helpers ----------------------------------------------------
    def resolve_alias(self, task: str, alias: str) -> str:
        """Aspect extraction is always grounded (RAG): haiku -> haiku_with_rag."""
        if task == "aspect_extraction" and not self.config.model(alias).rag:
            return f"{alias}_with_rag"
        return alias

    def make_fallback(self, review_id, task, alias, reason, decision="", parent_task=None) -> TaskResult:
        spec = self.config.model(alias)
        return TaskResult(
            review_id=review_id, task=task, model=alias, model_version=spec.model_id,
            result=fallback_payload(task), confidence=0.0, fallback=True,
            failure_reason=reason, model_decision=decision, parent_task=parent_task,
        )

    def _build_prompt(self, task, text, context) -> str:
        if task == "aspect_extraction":
            known = [a for a, _ in self.retriever.search(text)]
            return PROMPTS[task].format(
                text=text, known_aspects=json.dumps(known),
                sentiment_context=json.dumps(context) if context else "none",
            )
        return PROMPTS[task].format(text=text)

    def _finish(self, res: TaskResult, started: float) -> TaskResult:
        res.latency_ms = round((self._clock() - started) * 1000, 3)
        self.cost.record(CallRecord(
            timestamp=utc_now_iso(), task=res.task, model=res.model, review_id=res.review_id,
            input_tokens=res.input_tokens, output_tokens=res.output_tokens, cost_usd=res.cost_usd,
            latency_ms=res.latency_ms, model_decision=res.model_decision, cache_hit=res.cache_hit,
            confidence=res.confidence, fallback=res.fallback, model_version=res.model_version,
        ))
        return res

    # ---- main entry -------------------------------------------------
    def call(
        self, task: str, alias: str, review_id: str, text: str,
        context: Optional[dict] = None, decision: str = "", parent_task: Optional[str] = None,
    ) -> TaskResult:
        started = self._clock()
        alias = self.resolve_alias(task, alias)
        spec = self.config.model(alias)
        policy = self.config.tasks[task]
        breaker = self.breakers.get(spec.base_alias)

        try:
            text = validate_review_input(text)
        except ContractViolation as e:
            self.record_contract_violation()
            self.dlq.enqueue(review_id, task, str(e))
            return self._finish(self.make_fallback(review_id, task, alias, "invalid_input", decision, parent_task), started)

        # 1. cache (content hash; hit = no model call, zero cost)
        key = ReviewCache.make_key(task, alias, text, spec.model_id) if self.cache else None
        if self.cache:
            cached = self.cache.get(key)
            if cached is not None:
                try:
                    data = json.loads(cached)
                    result, confidence = data["result"], float(data["confidence"])
                except (ValueError, TypeError, KeyError) as e:
                    # corrupt / foreign entry: fail open and treat as a miss
                    logger.warning("unreadable cache entry %s: %s", key, e)
                    self.cache.record_miss(demote_hit=True)
                else:
                    return self._finish(TaskResult(
                        review_id=review_id, task=task, model=alias, model_version=spec.model_id,
                        result=result, confidence=confidence, cache_hit=True,
                        model_decision=decision, parent_task=parent_task,
                    ), started)

        # 2. circuit breaker
        if not breaker.allow_request():
            breaker.record_fallback()
            self.retry_queue.enqueue(
                review_id, task, "circuit_open", {"model": alias},
                delay_s=self.config.breaker.retry_after_s,
            )
            return self._finish(self.make_fallback(
                review_id, task, alias, "circuit_open", decision, parent_task), started)

        # 3. call with timeout + retries
        prompt = self._build_prompt(task, text, context)
        attempts = policy.retries + 1
        response, last_err = None, None
        for attempt in range(attempts):
            try:
                response = self.client.messages.create(
                    model=spec.model_id, max_tokens=MAX_TOKENS, timeout=policy.timeout_s,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.APITimeoutError as e:
                last_err = ("timeout", e)
                breaker.record_failure()
                if attempt == attempts - 1 or breaker.state == OPEN:
                    break  # no backoff after the last attempt; stop if the breaker tripped
                self._sleep(0.2 * 2 ** attempt)
            except anthropic.RateLimitError as e:
                last_err = ("rate_limit", e)
                breaker.record_failure()
                break  # do not hammer a rate-limited API
            except Exception as e:
                last_err = ("api_error", e)
                breaker.record_failure()
                break
        if response is None:
            breaker.record_fallback()
            reason, err = last_err
            logger.warning("model call failed (%s): %s", reason, err)
            self.retry_queue.enqueue(review_id, task, reason, {"model": alias},
                                     delay_s=self.config.breaker.retry_after_s)
            return self._finish(self.make_fallback(
                review_id, task, alias, reason, decision, parent_task), started)

        in_tok, out_tok = response.usage.input_tokens, response.usage.output_tokens
        cost = spec.cost(in_tok, out_tok)

        # 4. parse + contract validation
        try:
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            payload = validate_task_output(task, json.loads(raw))
        except (json.JSONDecodeError, ContractViolation, IndexError, AttributeError) as e:
            self.record_contract_violation()
            breaker.record_success()  # API worked; the output was bad
            breaker.record_fallback()
            self.dlq.enqueue(review_id, task, f"contract_violation: {e}", {"model": alias})
            res = self.make_fallback(review_id, task, alias, "contract_violation", decision, parent_task)
            res.input_tokens, res.output_tokens, res.cost_usd = in_tok, out_tok, cost
            return self._finish(res, started)

        breaker.record_success()
        res = TaskResult(
            review_id=review_id, task=task, model=alias, model_version=spec.model_id,
            result=payload, confidence=payload["confidence"], input_tokens=in_tok,
            output_tokens=out_tok, cost_usd=cost, model_decision=decision, parent_task=parent_task,
        )
        if self.cache:
            self.cache.set(key, json.dumps({"result": payload, "confidence": res.confidence}))
        return self._finish(res, started)
