import json

import pytest

from src.cost_tracker import CallRecord, CostTracker


def rec(task="sentiment", model="haiku", cost=0.001, lat=200, cache=False, rid="r1", fb=False):
    return CallRecord("2026-01-01T00:00:00Z", task, model, rid, 120, 15, cost, lat,
                      "decision", cache, 0.9, fb)


def test_record_has_article_fields():
    d = json.loads(rec().to_json())
    for k in ("timestamp", "task", "model", "review_id", "input_tokens", "output_tokens",
              "cost_usd", "latency_ms", "model_decision", "cache_hit", "confidence"):
        assert k in d


def test_summary_groups_by_task_and_model():
    t = CostTracker()
    for _ in range(3):
        t.record(rec(model="haiku", cost=0.0005, lat=200))
    t.record(rec(model="sonnet", cost=0.002, lat=800))
    t.record(rec(task="toxicity_detection", model="opus", cost=0.008, lat=1500))
    s = t.summary()
    assert s["sentiment"]["haiku"]["calls"] == 3
    assert s["sentiment"]["haiku"]["cost_usd"] == pytest.approx(0.0015)
    assert s["sentiment"]["sonnet"]["avg_latency_ms"] == 800
    assert s["toxicity_detection"]["opus"]["cost_usd"] == 0.008
    assert t.total_cost() == pytest.approx(0.0115)


def test_cache_hits_excluded_from_latency_but_counted():
    t = CostTracker()
    t.record(rec(lat=300))
    t.record(rec(lat=0, cost=0, cache=True))
    s = t.summary()["sentiment"]["haiku"]
    assert s["cache_hits"] == 1 and s["avg_latency_ms"] == 300


def test_cost_for_review():
    t = CostTracker()
    t.record(rec(rid="a", cost=0.1))
    t.record(rec(rid="a", cost=0.2))
    t.record(rec(rid="b", cost=1))
    assert t.cost_for_review("a") == pytest.approx(0.3)


def test_share_anomaly_detects_opus_spike():
    t = CostTracker()
    for i in range(10):
        t.record(rec(task="toxicity_detection", model="opus" if i < 6 else "haiku"))
    assert t.model_share("toxicity_detection", "opus") == 0.6
    assert "60%" in t.share_anomaly("toxicity_detection", "opus", expected=0.05)
    assert t.share_anomaly("toxicity_detection", "opus", expected=0.55) is None
