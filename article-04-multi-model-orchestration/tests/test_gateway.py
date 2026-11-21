"""Gateway middleware: cache, breaker, retries, contracts, cost tracking."""

import anthropic
import httpx
import pytest

from src.circuit_breaker import OPEN
from tests.conftest import FakeAnthropic, make_stack

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def timeout_err():
    return anthropic.APITimeoutError(request=REQ)


def rate_limit_err():
    return anthropic.RateLimitError("429", response=httpx.Response(429, request=REQ), body=None)


def test_success_records_cost_tokens_and_version(stack, config):
    r = stack.gateway.call("sentiment", "haiku", "r1", "Great product", decision="d")
    assert not r.fallback and r.confidence == 0.92
    assert (r.input_tokens, r.output_tokens) == (120, 15)
    assert r.cost_usd == pytest.approx(0.000195)
    assert r.model_version == config.model("haiku").model_id
    rec = stack.gateway.cost.records[0]
    assert rec.cost_usd == r.cost_usd and rec.model_decision == "d" and not rec.cache_hit
    assert stack.client.calls[0]["timeout"] == 1  # per-task timeout passed to the API


def test_cache_hit_skips_model_and_costs_nothing(stack):
    first = stack.gateway.call("sentiment", "haiku", "r1", "Same text")
    second = stack.gateway.call("sentiment", "haiku", "r2_dupe", "same   TEXT")
    assert len(stack.client.calls) == 1
    assert second.cache_hit and second.cost_usd == 0 and second.result == first.result
    assert second.review_id == "r2_dupe"  # keyed by content, not review_id
    assert stack.gateway.cache.stats() == {"hits": 1, "misses": 1, "hit_rate": 0.5}


def test_cache_not_shared_across_tasks_or_models(stack):
    stack.gateway.call("sentiment", "haiku", "r1", "x text")
    stack.gateway.call("toxicity_detection", "haiku", "r1", "x text")
    stack.gateway.call("sentiment", "sonnet", "r1", "x text")
    assert len(stack.client.calls) == 3


def test_fallback_results_are_not_cached(config):
    client = FakeAnthropic(lambda m, t, x: "not json")
    gw, _, _ = make_stack(client, config)
    gw.call("sentiment", "haiku", "r1", "text")
    gw.call("sentiment", "haiku", "r1", "text")
    assert len(client.calls) == 2


def test_markdown_wrapped_json_is_parsed(config):
    client = FakeAnthropic(lambda m, t, x: '```json\n{"sentiment":"neutral","confidence":0.8,"reasoning":"ok"}\n```')
    gw, _, _ = make_stack(client, config)
    assert gw.call("sentiment", "haiku", "r1", "meh").result["sentiment"] == "neutral"


def test_contract_violation_goes_to_dlq_with_fallback_and_charged_tokens(config):
    client = FakeAnthropic(lambda m, t, x: {"sentiment": "angry", "confidence": 0.9, "reasoning": "x"})
    gw, _, _ = make_stack(client, config)
    r = gw.call("sentiment", "haiku", "r1", "text")
    assert r.fallback and r.failure_reason == "contract_violation" and r.confidence == 0.0
    assert r.result == {"sentiment": "unknown", "confidence": 0.0, "needs_review": True}
    assert r.cost_usd > 0  # tokens were still spent
    assert gw.contract_violations == 1 and len(gw.dlq) == 1


def test_empty_input_rejected_before_any_call(stack):
    r = stack.gateway.call("sentiment", "haiku", "r1", "   ")
    assert r.fallback and r.failure_reason == "invalid_input"
    assert stack.client.calls == [] and len(stack.gateway.dlq) == 1


def test_timeout_retries_per_policy_then_succeeds(config):
    state = {"n": 0}

    def handler(m, t, x):
        state["n"] += 1
        if state["n"] <= 2:
            raise timeout_err()
        return None

    sleeps = []
    gw, _, _ = make_stack(FakeAnthropic(handler), config, sleeps=sleeps)
    r = gw.call("sentiment", "haiku", "r1", "text")  # sentiment: retries=2 -> 3 attempts
    assert not r.fallback and state["n"] == 3
    assert sleeps == [0.2, 0.4]


def test_aspect_extraction_retries_only_once(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(timeout_err()))
    gw, _, _ = make_stack(client, config)
    r = gw.call("aspect_extraction", "sonnet", "r1", "text")
    assert r.fallback and r.failure_reason == "timeout" and len(client.calls) == 2


def test_rate_limit_falls_back_without_retrying_and_queues_for_1h(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(rate_limit_err()))
    gw, _, clock = make_stack(client, config)
    r = gw.call("sentiment", "haiku", "r1", "text")
    assert r.fallback and r.failure_reason == "rate_limit" and len(client.calls) == 1
    item = gw.retry_queue.items[0]
    assert item.visible_at == clock.t + 3600
    assert gw.retry_queue.due() == []
    clock.advance(3600)
    assert len(gw.retry_queue.due()) == 1


def test_circuit_opens_after_5_failures_then_short_circuits(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(rate_limit_err()))
    gw, _, clock = make_stack(client, config)
    for i in range(5):
        gw.call("sentiment", "haiku", f"r{i}", f"text {i}")
    assert gw.breakers.get("haiku").state == OPEN
    calls_before = len(client.calls)
    r = gw.call("sentiment", "haiku", "r9", "text 9")
    assert r.fallback and r.failure_reason == "circuit_open"
    assert len(client.calls) == calls_before  # no API call while open
    assert r.result["sentiment"] == "unknown" and r.result["needs_review"] is True


def test_breaker_is_per_model_and_shared_across_rag_alias(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(rate_limit_err()) if "haiku" in m else None)
    gw, _, _ = make_stack(client, config)
    for i in range(5):
        gw.call("sentiment", "haiku", f"r{i}", f"t{i}")
    assert gw.call("sentiment", "sonnet", "x", "other").fallback is False
    assert gw.call("aspect_extraction", "haiku", "x", "other2").failure_reason == "circuit_open"


def test_circuit_recovers_after_60s(config):
    state = {"fail": True}

    def handler(m, t, x):
        if state["fail"]:
            raise rate_limit_err()

    gw, _, clock = make_stack(FakeAnthropic(handler), config)
    for i in range(5):
        gw.call("sentiment", "haiku", f"r{i}", f"t{i}")
    state["fail"] = False
    clock.advance(60)
    r = gw.call("sentiment", "haiku", "probe", "probe text")
    assert not r.fallback
    assert gw.breakers.get("haiku").state == "half_open"


def test_fallback_rate_alert_over_5_percent(config):
    state = {"fail": False}

    def handler(m, t, x):
        if state["fail"]:
            raise rate_limit_err()

    gw, _, _ = make_stack(FakeAnthropic(handler), config)
    for i in range(94):
        gw.call("sentiment", "haiku", f"r{i}", f"ok {i}")
    state["fail"] = True
    for i in range(4):  # 4/98 = 4.1% fallback rate: below alert
        gw.call("sentiment", "haiku", f"f{i}", f"bad {i}")
    assert gw.breakers.alerts() == []
    gw.call("sentiment", "haiku", "f9", "bad 9")
    assert gw.breakers.alerts() == ["haiku"]  # 5/99 = 5.05% > 5%


def test_aspect_prompt_is_grounded_with_retrieved_aspects_and_sentiment_context(stack):
    stack.gateway.call("aspect_extraction", "haiku", "r1", "the battery died",
                       context={"sentiment": "negative", "confidence": 0.9})
    call = stack.client.calls[0]
    assert '["battery"]' in call["prompt"] and '"negative"' in call["prompt"]
    assert "haiku" in call["model"]


def test_unexpected_exception_becomes_fallback_not_crash(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(RuntimeError("boom")))
    gw, _, _ = make_stack(client, config)
    r = gw.call("sentiment", "haiku", "r1", "text")
    assert r.fallback and r.failure_reason == "api_error"


def test_no_backoff_sleep_after_final_failed_attempt(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(timeout_err()))
    sleeps = []
    gw, _, _ = make_stack(client, config, sleeps=sleeps)
    r = gw.call("sentiment", "haiku", "r1", "text")  # 3 attempts, 2 backoffs
    assert r.fallback and r.failure_reason == "timeout" and len(client.calls) == 3
    assert sleeps == [0.2, 0.4]


def test_retries_stop_once_breaker_opens(config):
    client = FakeAnthropic(lambda m, t, x: (_ for _ in ()).throw(timeout_err()))
    gw, _, _ = make_stack(client, config)
    b = gw.breakers.get("haiku")
    for _ in range(4):
        b.record_failure()
    r = gw.call("sentiment", "haiku", "r1", "text")
    assert b.state == OPEN and r.fallback
    assert len(client.calls) == 1  # 5th failure tripped the breaker: no further retries


def test_cache_is_not_reused_across_model_versions(config, monkeypatch):
    """Re-running after a model upgrade must call the new model, not serve old results."""
    from src.config import load_router_config
    client = FakeAnthropic()
    gw, _, _ = make_stack(client, config)
    old = gw.call("sentiment", "haiku", "r1", "Great product")
    monkeypatch.setenv("HAIKU_MODEL", "claude-haiku-next")
    new_cfg = load_router_config()
    gw.config = new_cfg
    new = gw.call("sentiment", "haiku", "r1", "Great product")
    assert len(client.calls) == 2 and not new.cache_hit
    assert client.calls[1]["model"] == "claude-haiku-next"
    assert (old.model_version, new.model_version) == (config.model("haiku").model_id, "claude-haiku-next")


def test_corrupt_cache_entry_is_a_miss_not_a_crash(stack):
    from src.cache import ReviewCache
    spec = stack.gateway.config.model("haiku")
    key = ReviewCache.make_key("sentiment", "haiku", "Great product", spec.model_id)
    stack.gateway.cache.set(key, "{not json")
    r = stack.gateway.call("sentiment", "haiku", "r1", "Great product")
    assert not r.fallback and not r.cache_hit and len(stack.client.calls) == 1
    assert stack.gateway.cache.stats()["hits"] == 0 and stack.gateway.cache.stats()["misses"] == 1
    again = stack.gateway.call("sentiment", "haiku", "r2", "Great product")
    assert again.cache_hit  # overwritten with a good entry
