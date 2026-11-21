"""The four orchestration patterns."""

import threading

import pytest

from tests.conftest import FakeAnthropic, make_stack

SIMPLE = {"review_id": "s1", "text": "This product is great. 5 stars."}
COMPLEX = {"review_id": "c1", "text": "I will file a lawsuit, the battery is terrible and the app is buggy."}
NEG = {"review_id": "n1", "text": "Terrible battery, the software is buggy."}
NEG_UNSAFE = {"review_id": "n2", "text": "Terrible battery, the support guy is an idiot."}
POS = {"review_id": "p1", "text": "Love the battery, wonderful."}


# ---- Pattern 1: Router ------------------------------------------------
def test_router_simple_uses_haiku_for_all_three_tasks(stack):
    out = stack.orch.run_router(SIMPLE)
    assert set(out.results) == {"sentiment", "aspect_extraction", "toxicity_detection"}
    assert all("haiku" in c["model"] for c in stack.client.calls) and len(stack.client.calls) == 3
    assert "router_pattern=simple" in out.results["sentiment"].model_decision


def test_router_complex_uses_sonnet_for_all_three_tasks(stack, config):
    out = stack.orch.run_router(COMPLEX)
    assert {c["model"] for c in stack.client.calls} == {config.model("sonnet").model_id}
    assert out.results["aspect_extraction"].model == "sonnet_with_rag"
    assert "router_pattern=complex" in out.results["sentiment"].model_decision


# ---- Pattern 2: Chain -------------------------------------------------
def test_chain_positive_stops_after_sentiment(stack):
    out = stack.orch.run_chain(POS)
    assert list(out.results) == ["sentiment"] and len(stack.client.calls) == 1
    assert "haiku" in stack.client.calls[0]["model"]


def test_chain_negative_runs_aspects_but_not_toxicity(stack):
    out = stack.orch.run_chain(NEG)
    assert list(out.results) == ["sentiment", "aspect_extraction"]
    assert out.results["aspect_extraction"].parent_task == "sentiment"
    assert "sonnet" in stack.client.calls[1]["model"]
    assert '"negative"' in stack.client.calls[1]["prompt"]  # sentiment context in prompt


def test_chain_unsafe_aspects_trigger_opus(stack, config):
    out = stack.orch.run_chain(NEG_UNSAFE)
    assert list(out.results) == ["sentiment", "aspect_extraction", "toxicity_detection"]
    assert stack.client.calls[2]["model"] == config.model("opus").model_id
    assert out.results["toxicity_detection"].parent_task == "aspect_extraction"


def test_chain_stops_and_flags_review_when_sentiment_falls_back(config):
    client = FakeAnthropic(lambda m, t, x: "garbage")
    gw, orch, _ = make_stack(client, config)
    out = orch.run_chain(NEG)
    assert list(out.results) == ["sentiment"] and out.needs_human_review
    assert out.results["sentiment"].fallback


def test_chain_call_mix_matches_cost_logic(config):
    """Cheap-first chain: expensive calls are rare when most reviews are positive."""
    client = FakeAnthropic()
    gw, orch, _ = make_stack(client, config)
    reviews = [{"review_id": f"p{i}", "text": f"Lovely item number {i}"} for i in range(6)]
    reviews += [{"review_id": f"n{i}", "text": f"Terrible item number {i}"} for i in range(3)]
    reviews += [{"review_id": "u", "text": "Terrible, the clerk is an idiot"}]
    for r in reviews:
        orch.run_chain(r)
    assert len(client.calls_for("haiku")) == 10
    assert len(client.calls_for("sonnet")) == 4
    assert len(client.calls_for("opus")) == 1


# ---- Pattern 3: Fan-out / fan-in --------------------------------------
def test_fan_out_runs_three_models_concurrently(config):
    barrier = threading.Barrier(3, timeout=3)  # only passes if all 3 calls overlap

    def handler(m, t, x):
        barrier.wait()

    client = FakeAnthropic(handler)
    gw, orch, _ = make_stack(client, config)
    out = orch.run_fan_out(POS)
    assert not any(r.fallback for r in out.results.values())
    models = {c["task"]: c["model"] for c in client.calls}
    assert "haiku" in models["sentiment"] and "sonnet" in models["aspect_extraction"]
    assert "opus" in models["toxicity_detection"]
    assert not out.needs_human_review


def test_fan_out_merges_partial_failure_as_fallback(config):
    def handler(m, t, x):
        if t == "toxicity_detection":
            return "garbage"

    gw, orch, _ = make_stack(FakeAnthropic(handler), config)
    out = orch.run_fan_out(POS)
    assert out.results["toxicity_detection"].fallback
    assert not out.results["sentiment"].fallback and out.needs_human_review


def test_fan_out_costs_more_than_chain_for_positive_review(config):
    c1, c2 = FakeAnthropic(), FakeAnthropic()
    _, o1, _ = make_stack(c1, config)
    _, o2, _ = make_stack(c2, config)
    assert o1.run_fan_out(POS).total_cost > o2.run_chain(POS).total_cost


# ---- Pattern 4: Fallback cascade --------------------------------------
def conf_handler(haiku_conf, sonnet_conf):
    def handler(model, task, text):
        conf = haiku_conf if "haiku" in model else sonnet_conf
        return {"aspects": ["battery"], "unsafe_language": False, "confidence": conf, "reasoning": "x"}
    return handler


def test_cascade_confident_haiku_stops_at_step_1(config):
    client = FakeAnthropic(conf_handler(0.9, 0.9))
    _, orch, _ = make_stack(client, config)
    out = orch.run_cascade(POS)
    assert len(client.calls) == 1 and out.results["aspect_extraction"].model == "haiku_with_rag"
    assert not out.needs_human_review


def test_cascade_threshold_is_inclusive_at_0_7(config):
    client = FakeAnthropic(conf_handler(0.7, 0.9))
    _, orch, _ = make_stack(client, config)
    orch.run_cascade(POS)
    assert len(client.calls) == 1


def test_cascade_escalates_to_sonnet_when_low_confidence(config):
    client = FakeAnthropic(conf_handler(0.69, 0.85))
    _, orch, _ = make_stack(client, config)
    out = orch.run_cascade(POS)
    assert [("haiku" in c["model"]) for c in client.calls] == [True, False]
    res = out.results["aspect_extraction"]
    assert res.model == "sonnet_with_rag" and res.confidence == 0.85 and not res.fallback
    assert "cascade_step_2" in res.model_decision


def test_cascade_exhausted_returns_fallback_and_queues_human_review(config):
    client = FakeAnthropic(conf_handler(0.3, 0.5))
    gw, orch, _ = make_stack(client, config)
    out = orch.run_cascade(POS)
    res = out.results["aspect_extraction"]
    assert res.fallback and res.failure_reason == "low_confidence_after_cascade"
    assert out.needs_human_review and len(orch.human_queue) == 1
    assert orch.human_queue.items[0].review_id == "p1"


def test_cascade_escalates_when_cheap_model_errors(config):
    def handler(model, task, text):
        if "haiku" in model:
            return "not json"
        return {"aspects": [], "unsafe_language": False, "confidence": 0.9, "reasoning": "x"}

    client = FakeAnthropic(handler)
    _, orch, _ = make_stack(client, config)
    out = orch.run_cascade(POS)
    assert out.results["aspect_extraction"].model == "sonnet_with_rag"


# ---- cross-cutting ----------------------------------------------------
def test_pattern_latency_recorded_per_pattern(config):
    from tests.conftest import FakeClock
    clock = FakeClock()

    def handler(m, t, x):
        clock.advance(0.25)  # each model call takes 250ms

    client = FakeAnthropic(handler)
    gw, orch, _ = make_stack(client, config, clock=clock)
    out = orch.run_router(SIMPLE)
    assert out.latency_ms == pytest.approx(750)  # 3 sequential calls
    assert orch.recorder.report("router")["p50"] == pytest.approx(750)
    assert orch.recorder.report("chain") == {"count": 0}


def test_run_dispatch_and_unknown_pattern(stack):
    assert stack.orch.run("cascade", POS).pattern == "cascade"
    with pytest.raises(KeyError):
        stack.orch.run("nope", POS)


def test_every_pattern_logs_cost_records_with_decisions(stack):
    stack.orch.run_chain(NEG)
    recs = stack.gateway.cost.records
    assert [r.task for r in recs] == ["sentiment", "aspect_extraction"]
    assert all(r.model_decision for r in recs)


def test_cascade_total_cost_includes_escalated_calls(config):
    client = FakeAnthropic(conf_handler(0.5, 0.9))
    gw, orch, _ = make_stack(client, config)
    out = orch.run_cascade(POS)
    assert len(client.calls) == 2
    assert out.total_cost == pytest.approx(gw.cost.total_cost())
    assert out.total_cost > out.results["aspect_extraction"].cost_usd


def test_cascade_exhausted_still_reports_money_spent(config):
    client = FakeAnthropic(conf_handler(0.3, 0.5))
    gw, orch, _ = make_stack(client, config)
    out = orch.run_cascade(POS)
    assert out.results["aspect_extraction"].cost_usd == 0.0
    assert out.total_cost == pytest.approx(gw.cost.total_cost()) and out.total_cost > 0


def test_chain_low_confidence_toxicity_flags_human_review(config):
    def handler(m, t, x):
        if t == "toxicity_detection":
            return {"toxic": True, "categories": [], "confidence": 0.6, "reasoning": "unsure"}

    _, orch, _ = make_stack(FakeAnthropic(handler), config)
    out = orch.run_chain(NEG_UNSAFE)
    assert "toxicity_detection" in out.results and out.needs_human_review


def test_chain_toxicity_fallback_flags_human_review(config):
    def handler(m, t, x):
        if t == "toxicity_detection":
            return "garbage"

    _, orch, _ = make_stack(FakeAnthropic(handler), config)
    out = orch.run_chain(NEG_UNSAFE)
    assert out.results["toxicity_detection"].fallback and out.needs_human_review


def test_chain_confident_toxicity_does_not_flag(stack):
    out = stack.orch.run_chain(NEG_UNSAFE)
    assert not out.needs_human_review
