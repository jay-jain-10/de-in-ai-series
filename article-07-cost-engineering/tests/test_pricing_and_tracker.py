import pytest
from datetime import datetime, timezone
from tests.conftest import FakeAnthropic, make_response
from src.config import AnthropicConfig
from src.pricing import compute_cost_usd, model_cost_ratio, price_for
from src.cost_events import CostEvent, annotate_row, InMemorySink, FirehoseSink, SnowflakeCostSink
from src.cost_tracker import CostTrackingClient
from src.cost_sla import SLAEnforcer, CostBudgetExceeded
from src.config import CostSLAConfig
from unittest.mock import MagicMock

CFG = AnthropicConfig()


def test_opus_vs_haiku_is_18_75x():
    assert model_cost_ratio("opus", "haiku") == pytest.approx(18.75)


def test_input_prices_match_article():
    assert compute_cost_usd(CFG.opus_model, 1_000_000, 0) == 15
    assert compute_cost_usd(CFG.sonnet_model, 1_000_000, 0) == 3
    assert compute_cost_usd(CFG.haiku_model, 1_000_000, 0) == pytest.approx(0.80)


def test_batch_halves_cost_and_alias_lookup():
    full = compute_cost_usd(CFG.sonnet_model, 1000, 100)
    assert compute_cost_usd(CFG.sonnet_model, 1000, 100, batch=True) == pytest.approx(full / 2)
    assert price_for("claude-haiku-4-5").tier == "haiku"
    with pytest.raises(KeyError):
        price_for("gpt-4")


def test_tracker_emits_event_with_cost_fields():
    sink = InMemorySink()
    t = CostTrackingClient(client=FakeAnthropic([make_response(inp=1000, out=100)]), sink=sink)
    r = t.complete("hi", model=CFG.haiku_model, pipeline_name="p", team_name="t", record_id="r1")
    ev = sink.events[0]
    assert ev.cost_tokens == 1100 == r.cost_tokens
    assert ev.cost_usd == pytest.approx(1000 / 1e6 * 0.8 + 100 / 1e6 * 4)
    assert (ev.pipeline_name, ev.team_name, ev.attempt, ev.is_retry) == ("p", "t", 1, False)


def test_system_prompt_forwarded_and_failed_validator_marks_waste():
    fake = FakeAnthropic([make_response(text="not json")])
    t = CostTrackingClient(client=fake)
    r = t.complete("x", model=CFG.haiku_model, pipeline_name="p", system="SYS",
                   validator=lambda s: s.startswith("{"))
    assert fake.calls[0]["system"] == "SYS"
    assert r.event.success is False


def test_retry_events_count_toward_row_cost():
    sink = InMemorySink()
    t = CostTrackingClient(client=FakeAnthropic(), sink=sink)
    a = t.complete("x", model=CFG.haiku_model, pipeline_name="p", record_id="r")
    b = t.complete("x", model=CFG.haiku_model, pipeline_name="p", record_id="r", attempt=2)
    assert b.event.is_retry and not a.event.is_retry
    row = annotate_row({"id": "r"}, [a.event, b.event])
    assert row["cost_attempts"] == 2
    assert row["cost_usd"] == pytest.approx(a.cost_usd * 2)
    assert row["cost_tokens"] == a.cost_tokens * 2


def test_daily_budget_pauses_pipeline():
    enforcer = SLAEnforcer(CostSLAConfig(max_daily_cost_usd=0.001))
    now = lambda: datetime(2026, 3, 1, tzinfo=timezone.utc)
    t = CostTrackingClient(client=FakeAnthropic([make_response(inp=10000, out=1000)]),
                           enforcer=enforcer, now=now)
    t.complete("x", model=CFG.opus_model, pipeline_name="p")
    with pytest.raises(CostBudgetExceeded):
        t.complete("y", model=CFG.opus_model, pipeline_name="p")
    # a different pipeline is unaffected
    t.complete("y", model=CFG.haiku_model, pipeline_name="other")


def test_firehose_sink_batches_and_snowflake_sink_inserts():
    fh = MagicMock()
    evs = [CostEvent("p", "m", 1, 1, 0.1) for _ in range(501)]
    FirehoseSink("stream", client=fh).write(evs)
    assert fh.put_record_batch.call_count == 2
    assert fh.put_record_batch.call_args_list[0].kwargs["DeliveryStreamName"] == "stream"

    conn = MagicMock()
    SnowflakeCostSink(conn).write(evs[:2])
    cur = conn.cursor.return_value
    assert "cost_events" in cur.executemany.call_args.args[0]
    assert len(cur.executemany.call_args.args[1]) == 2
    conn.commit.assert_called_once()
    cur.close.assert_called_once()


def test_firehose_partial_failure_retried_then_raises():
    fh = MagicMock()
    evs = [CostEvent("p", "m", 1, 1, 0.1, record_id=str(i)) for i in range(3)]
    ok = {"FailedPutCount": 0, "RequestResponses": [{"RecordId": "x"}]}
    partial = {"FailedPutCount": 1, "RequestResponses": [
        {"RecordId": "a"}, {"ErrorCode": "ServiceUnavailableException"}, {"RecordId": "c"}]}
    fh.put_record_batch.side_effect = [partial, ok]
    FirehoseSink("s", client=fh).write(evs)
    assert fh.put_record_batch.call_count == 2
    retried = fh.put_record_batch.call_args_list[1].kwargs["Records"]
    assert len(retried) == 1 and b'"record_id": "1"' in retried[0]["Data"]

    fh2 = MagicMock()
    fh2.put_record_batch.side_effect = lambda **kw: {
        "FailedPutCount": len(kw["Records"]),
        "RequestResponses": [{"ErrorCode": "InternalFailure"}] * len(kw["Records"])}
    with pytest.raises(RuntimeError):
        FirehoseSink("s", client=fh2, max_retries=1).write(evs)
    assert fh2.put_record_batch.call_count == 2


def test_spend_observed_even_if_sink_fails():
    class BrokenSink:
        def write(self, events):
            raise ConnectionError("firehose down")
    enforcer = SLAEnforcer()
    now = lambda: datetime(2026, 3, 1, tzinfo=timezone.utc)
    t = CostTrackingClient(client=FakeAnthropic([make_response(inp=1_000_000, out=0)]),
                           sink=BrokenSink(), enforcer=enforcer, now=now)
    with pytest.raises(ConnectionError):
        t.complete("x", model=CFG.sonnet_model, pipeline_name="p")
    assert enforcer.daily_spend("p", "2026-03-01") == pytest.approx(3.0)
