import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from src.config import AnthropicConfig
from src.backfill_estimator import (estimate_backfill, estimate_from_history, require_approval,
                                    BackfillNotApproved)
from src.cost_events import CostEvent
from src.metrics import MetricEmitter, alarm_definitions, create_alarms

M = AnthropicConfig()


def test_backfill_100k_formula():
    e = estimate_backfill(100_000, 1000, 100, M.sonnet_model)
    per = 1000 / 1e6 * 3 + 100 / 1e6 * 15
    assert e.estimated_cost_usd == pytest.approx(per * 100_000)
    assert e.exceeds_daily_budget            # $450 > $200
    assert not e.exceeds_record_sla          # $0.0045 < $0.005


def test_backfill_batch_and_cache_reduce_estimate():
    full = estimate_backfill(100_000, 1000, 100, M.sonnet_model)
    cheap = estimate_backfill(100_000, 1000, 100, M.sonnet_model, batch=True, cache_hit_rate=0.4)
    assert cheap.estimated_cost_usd == pytest.approx(full.estimated_cost_usd * 0.5 * 0.6)


def test_backfill_approval_gate():
    e = estimate_backfill(100_000, 1000, 100, M.sonnet_model)
    with pytest.raises(BackfillNotApproved):
        require_approval(e)
    require_approval(e, approved_by="finance")
    small = estimate_backfill(100, 100, 10, M.haiku_model)
    require_approval(small)


def test_backfill_from_history_ignores_cache_hits():
    rows = [{"input_tokens": 1000, "output_tokens": 100, "cache_hit": False},
            {"input_tokens": 3000, "output_tokens": 300, "cache_hit": False},
            {"input_tokens": 0, "output_tokens": 0, "cache_hit": True}]
    e = estimate_from_history(rows, 1000, M.haiku_model)
    assert (e.avg_input_tokens, e.avg_output_tokens) == (2000, 200)


def test_backfill_from_history_counts_retries_per_record():
    # r1 needed a retry (2 paid calls), r2 one call: 3 calls, 2 records.
    rows = [{"record_id": "r1", "input_tokens": 1000, "output_tokens": 100, "cache_hit": False},
            {"record_id": "r1", "input_tokens": 1000, "output_tokens": 100, "cache_hit": False},
            {"record_id": "r2", "input_tokens": 1000, "output_tokens": 100, "cache_hit": False}]
    e = estimate_from_history(rows, 10_000, M.sonnet_model)
    assert (e.avg_input_tokens, e.avg_output_tokens) == (1500, 150)
    per = 1500 / 1e6 * 3 + 150 / 1e6 * 15      # $0.00675 by hand
    assert e.cost_per_record_usd == pytest.approx(per)
    assert e.estimated_cost_usd == pytest.approx(67.5)
    assert e.exceeds_record_sla                 # per-call average ($0.0045) would have hidden this
    with pytest.raises(ValueError):
        estimate_from_history([], 10, M.haiku_model)


def test_emitter_metrics_and_flush_chunks():
    cw = MagicMock()
    em = MetricEmitter(client=cw, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    em.observe(CostEvent("p1", "haiku", 100, 10, 1.0))
    em.observe(CostEvent("p1", "haiku", 0, 0, 0.0, cache_hit=True))
    em.observe(CostEvent("p2", "sonnet", 100, 30, 2.0, success=False))
    assert em.daily_cost == 3.0
    assert em.cache_hit_rate == pytest.approx(1 / 3)
    assert em.token_waste_rate == pytest.approx(30 / 40)
    n = em.flush()
    names = {d["MetricName"] for c in cw.put_metric_data.call_args_list
             for d in c.kwargs["MetricData"]}
    assert {"daily_cost", "cost_per_model", "cost_per_pipeline", "cache_hit_rate",
            "token_waste_rate"} == names
    assert n == 7
    assert cw.put_metric_data.call_args.kwargs["Namespace"] == "AIFinOps"


def test_alarms():
    defs = alarm_definitions(pipelines=["p1", "p2"])
    daily = next(d for d in defs if d["MetricName"] == "daily_cost")
    # flushes publish increments, so the daily total is the Sum over the 1-day period
    assert daily["Threshold"] == 200 and daily["Statistic"] == "Sum" and daily["Period"] == 86400
    per_pipe = [d for d in defs if d["MetricName"] == "cost_per_pipeline"]
    # metric is published with a Pipeline dimension; the alarm must carry the same one
    assert [d["Dimensions"] for d in per_pipe] == [[{"Name": "Pipeline", "Value": "p1"}],
                                                   [{"Name": "Pipeline", "Value": "p2"}]]
    assert all(d["Statistic"] == "Sum" for d in per_pipe)
    cw = MagicMock()
    assert create_alarms(cw, pipelines=["p1"]) == 4 == cw.put_metric_alarm.call_count


def test_alarm_dimensions_match_published_metric(cw_client):
    em = MetricEmitter(client=cw_client)
    em.observe(CostEvent("p1", "haiku", 100, 10, 1.0))
    published = {(d["MetricName"], tuple((x["Name"], x["Value"]) for x in d.get("Dimensions", [])))
                 for d in em.build_metric_data()}
    for a in alarm_definitions(pipelines=["p1"]):
        key = (a["MetricName"], tuple((x["Name"], x["Value"]) for x in a.get("Dimensions", [])))
        assert key in published, a["AlarmName"]


def test_flush_publishes_increments_not_cumulative(cw_client):
    em = MetricEmitter(client=cw_client)
    em.observe(CostEvent("p1", "haiku", 100, 10, 1.0))
    em.flush()
    em.observe(CostEvent("p1", "haiku", 100, 10, 2.0))
    em.flush()
    sent = [d["Value"] for c in cw_client.put_metric_data.call_args_list
            for d in c.kwargs["MetricData"] if d["MetricName"] == "daily_cost"]
    assert sent == [1.0, 2.0]          # Sum over the day = 3.0, no double counting
