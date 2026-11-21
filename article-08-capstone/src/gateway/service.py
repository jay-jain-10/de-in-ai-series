"""GatewayService: the platform's heartbeat.

Request flow: quota -> cache (L1 Redis, L2 S3) -> circuit breaker -> LLM call with retries
-> JSON-schema enforcement (retry with explicit instruction, then fall back to Haiku)
-> cache write -> cost event. Every call carries CallerMetadata.
"""

import json
import re
import time
import uuid
from typing import Any, Callable, Optional

from ..config import PlatformConfig
from ..contracts import (CallerMetadata, ClassifyRequest, ExtractRequest, GatewayResponse,
                         ScoreRequest, SummarizeRequest, TaskType, ValidateRequest)
from ..observability import MetricsRegistry
from .cache import TieredCache, cache_key
from .circuit_breaker import CircuitBreaker
from .cost import CostTracker
from .llm import LLMClient, LLMTransientError
from .prompts import PromptRegistry, default_registry
from .quota import QuotaManager, TeamAccount
from .router import ModelRouter

EXPLICIT_RETRY_SUFFIX = ("\n\nYour previous reply was not valid JSON for the required schema. "
                         "Reply with ONLY the JSON object, no prose, no code fences.")
SIMPLE_FALLBACK_SUFFIX = "\n\nKeep it simple: respond with the minimal JSON object matching the schema."


class InvalidOutput(ValueError):
    pass


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidOutput(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise InvalidOutput("expected a JSON object")
    return obj


def _conf(obj: dict) -> float:
    c = obj.get("confidence")
    if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0.0 <= c <= 1.0:
        raise InvalidOutput("confidence must be a number in [0, 1]")
    return float(c)


def _check_extract(schema: dict) -> Callable[[dict], dict]:
    def check(obj: dict) -> dict:
        fields = obj.get("fields")
        if not isinstance(fields, dict):
            raise InvalidOutput("'fields' must be an object")
        missing = [k for k in schema if k not in fields]
        if missing:
            raise InvalidOutput(f"missing fields: {missing}")
        return {"fields": {k: fields[k] for k in schema}, "confidence": _conf(obj)}
    return check


def _check_classify(categories: list) -> Callable[[dict], dict]:
    def check(obj: dict) -> dict:
        if obj.get("category") not in categories:
            raise InvalidOutput(f"category must be one of {categories}")
        return {"category": obj["category"], "confidence": _conf(obj),
                "reasoning": str(obj.get("reasoning", ""))}
    return check


def _check_summarize(obj: dict) -> dict:
    if not isinstance(obj.get("summary"), str) or not obj["summary"].strip():
        raise InvalidOutput("'summary' must be a non-empty string")
    return {"summary": obj["summary"]}


def _check_validate(obj: dict) -> dict:
    if not isinstance(obj.get("valid"), bool):
        raise InvalidOutput("'valid' must be boolean")
    issues = obj.get("issues", [])
    if not isinstance(issues, list):
        raise InvalidOutput("'issues' must be a list")
    return {"valid": obj["valid"], "issues": [str(i) for i in issues], "confidence": _conf(obj)}


def _check_score(obj: dict) -> dict:
    s = obj.get("score")
    if not isinstance(s, (int, float)) or isinstance(s, bool) or not 0.0 <= s <= 1.0:
        raise InvalidOutput("'score' must be a number in [0, 1]")
    return {"score": float(s), "rationale": str(obj.get("rationale", ""))}


class GatewayService:
    def __init__(self, config: PlatformConfig, llm: LLMClient, cache: TieredCache,
                 cost_tracker: CostTracker, router: ModelRouter, quota: QuotaManager,
                 breaker: Optional[CircuitBreaker] = None, metrics: Optional[MetricsRegistry] = None,
                 prompts: Optional[PromptRegistry] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config, self.llm, self.cache = config, llm, cache
        self.cost, self.router, self.quota = cost_tracker, router, quota
        gw = config.gateway
        self.breaker = breaker or CircuitBreaker(gw.breaker_error_threshold, gw.breaker_window,
                                                 gw.breaker_min_calls, gw.breaker_cooldown_seconds)
        self.metrics = metrics or MetricsRegistry()
        self.prompts = prompts or default_registry()
        self._sleep = sleep

    # ----- public task API (one method per endpoint) ---------------------------------

    def extract(self, req: ExtractRequest, account: TeamAccount) -> GatewayResponse:
        schema = "\n".join(f"- {k}: {v}" for k, v in req.extraction_schema.items())
        return self._run(TaskType.EXTRACT, req.caller, account, self.router.select(TaskType.EXTRACT),
                         {"schema": schema, "text": req.text},
                         {"extraction_schema": req.extraction_schema, "text": req.text},
                         _check_extract(req.extraction_schema),
                         default={"fields": {k: None for k in req.extraction_schema}, "confidence": 0.0})

    def classify(self, req: ClassifyRequest, account: TeamAccount) -> GatewayResponse:
        return self._run(TaskType.CLASSIFY, req.caller, account, self.router.select(TaskType.CLASSIFY),
                         {"categories": ", ".join(req.categories), "text": req.text},
                         {"categories": req.categories, "text": req.text},
                         _check_classify(req.categories),
                         default={"category": req.categories[0], "confidence": 0.0,
                                  "reasoning": "FALLBACK: gateway degraded"})

    def summarize(self, req: SummarizeRequest, account: TeamAccount) -> GatewayResponse:
        return self._run(TaskType.SUMMARIZE, req.caller, account, self.router.select(TaskType.SUMMARIZE),
                         {"max_words": req.max_words, "text": req.text},
                         {"max_words": req.max_words, "text": req.text},
                         _check_summarize, default={"summary": req.text[:200]})

    def validate(self, req: ValidateRequest, account: TeamAccount) -> GatewayResponse:
        return self._run(TaskType.VALIDATE, req.caller, account, self.router.select(TaskType.VALIDATE),
                         {"rules": "\n".join(f"- {r}" for r in req.rules),
                          "record": json.dumps(req.record, default=str, sort_keys=True)},
                         {"record": req.record, "rules": req.rules},
                         _check_validate,
                         default={"valid": True, "issues": [], "confidence": 0.0})

    def score(self, req: ScoreRequest, account: TeamAccount) -> GatewayResponse:
        return self._run(TaskType.SCORE, req.caller, account,
                         self.router.select(TaskType.SCORE, req.complexity),
                         {"scoring_logic": req.scoring_logic,
                          "record": json.dumps(req.record, default=str, sort_keys=True)},
                         {"record": req.record, "scoring_logic": req.scoring_logic},
                         _check_score, default={"score": 0.5, "rationale": "FALLBACK: gateway degraded"})

    # ----- core -------------------------------------------------------------------------

    def _run(self, task: TaskType, caller: CallerMetadata, account: TeamAccount, model: str,
             prompt_args: dict, cache_payload: dict, check: Callable[[dict], dict],
             default: dict) -> GatewayResponse:
        started = time.time()
        self.metrics.inc("gateway_requests_total", team=caller.team, task=task.value)
        self.quota.consume(account)   # raises QuotaExceeded -> HTTP 429
        prompt = self.prompts.get(task.value)
        request_id = uuid.uuid4().hex

        key = cache_key(task.value, model, prompt.version, cache_payload, scope=account.team)
        hit, tier = self.cache.get(key)
        if hit is not None:
            self.metrics.inc("gateway_cache_hits_total", team=caller.team, tier=tier)
            self.cost.record(caller, task, model, 0, 0, cached=True, prompt_version=prompt.version)
            return GatewayResponse(request_id=request_id, task=task, result=hit["result"],
                                   model=hit["model"], cached=True, cache_tier=tier,
                                   prompt_version=prompt.version)

        result, used_model, in_tok, out_tok, attempts, cost = self._call_with_retries(
            task, model, prompt.template.format(**prompt_args), check, caller, prompt.version)

        degraded = result is None
        if degraded:
            self.metrics.inc("gateway_degraded_total", team=caller.team, task=task.value)
            result, used_model = default, model
        else:
            self.cache.set(key, {"result": result, "model": used_model})
        self.metrics.observe("gateway_latency_seconds", time.time() - started, task=task.value)
        return GatewayResponse(request_id=request_id, task=task, result=result, model=used_model,
                               degraded=degraded, prompt_version=prompt.version,
                               input_tokens=in_tok, output_tokens=out_tok,
                               cost_usd=round(cost, 8), attempts=attempts)

    def _call_with_retries(self, task, model, prompt, check, caller, prompt_version):
        """Returns (result|None, model_used, in_tokens, out_tokens, attempts, cost_usd).

        Cost is summed per attempt at that attempt's model price: a Sonnet attempt followed by a
        Haiku fallback must not be priced entirely at Haiku rates."""
        max_retries = self.config.anthropic.max_retries
        plan: list[tuple[str, str]] = []
        for i in range(max_retries):
            plan.append((model, prompt if i == 0 else prompt + EXPLICIT_RETRY_SUFFIX))
        fallback = self.router.fallback()
        if fallback != model:
            plan.append((fallback, prompt + SIMPLE_FALLBACK_SUFFIX))

        in_tok = out_tok = attempts = 0
        cost = 0.0
        for i, (m, p) in enumerate(plan):
            if not self.breaker.allow():
                self.metrics.inc("gateway_breaker_rejections_total", team=caller.team)
                break
            attempts += 1
            self.metrics.inc("gateway_llm_calls_total", model=m, team=caller.team)
            try:
                res = self.llm.complete(m, p, self.config.anthropic.max_tokens)
            except LLMTransientError:
                self.breaker.record_failure()
                self.metrics.inc("gateway_llm_errors_total", model=m)
                if i < len(plan) - 1:          # no pointless back-off after the final attempt
                    self._sleep(min(2 ** i, 8))
                continue
            except Exception:
                # Non-transient (bad request, auth, SDK bug): do not retry and do not count it
                # toward the shared breaker, but release a half-open probe slot so the breaker
                # cannot get stuck waiting for a result that will never be recorded.
                self.breaker.release_probe()
                raise
            self.breaker.record_success()
            in_tok += res.input_tokens
            out_tok += res.output_tokens
            ev = self.cost.record(caller, task, m, res.input_tokens, res.output_tokens,
                                  cached=False, prompt_version=prompt_version)
            cost += ev.cost_usd
            try:
                return check(parse_json(res.text)), m, in_tok, out_tok, attempts, cost
            except InvalidOutput:
                self.metrics.inc("gateway_invalid_output_total", model=m, task=task.value)
        return None, model, in_tok, out_tok, attempts, cost
