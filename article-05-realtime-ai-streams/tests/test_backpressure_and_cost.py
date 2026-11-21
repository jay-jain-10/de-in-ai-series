import pytest

from src.backpressure import BackpressureController
from src.batch_queue import MicroBatchQueue
from src.config import AnthropicConfig, CostConfig
from src.cost_guard import CostGuard, sample_bucket
from src.metrics import Metrics, percentile
from src.sla import SLATracker
from tests.helpers import FakeClock


def test_queue_flushes_on_size():
    q = MicroBatchQueue(10, 2.0, clock=FakeClock())
    for i in range(9):
        q.put(i)
    assert not q.ready() and q.take() == []
    q.put(9)
    assert q.ready() and q.take() == list(range(10)) and len(q) == 0


def test_queue_flushes_on_time():
    c = FakeClock()
    q = MicroBatchQueue(10, 2.0, clock=c)
    q.put("a")
    c.advance(1.9)
    assert not q.ready()
    c.advance(0.2)
    assert q.take() == ["a"]


def test_queue_bounded_sheds_load():
    q = MicroBatchQueue(10, 2.0, max_pending=5, clock=FakeClock())
    results = [q.put(i) for i in range(8)]
    assert results.count(False) == 3 and q.dropped == 3


def test_backpressure_levels():
    m = Metrics()
    bp = BackpressureController(metrics=m)
    assert bp.update(100) == "normal" and (bp.max_events, bp.max_wait, bp.sample_rate) == (10, 2.0, 1.0)
    assert bp.update(5000) == "normal"  # threshold is strictly "above 5000"
    assert bp.update(5001) == "high"
    assert (bp.max_events, bp.max_wait, bp.sample_rate) == (50, 5.0, 1.0)
    assert bp.update(10_001) == "critical" and bp.sample_rate == 0.5
    assert m.gauges["consumer_lag_events"] == 10_001 and m.gauges["backpressure_level"] == 2
    assert bp.update(10) == "normal" and bp.sample_rate == 1.0 and not bp.stale


def test_lag_seconds_is_quality_signal():
    bp = BackpressureController()
    assert bp.update(50, lag_seconds=6.0) == "high" and bp.stale


def test_percentile_nearest_rank():
    vals = list(range(1, 101))
    assert percentile(vals, 50) == 50 and percentile(vals, 99) == 99 and percentile([], 99) == 0.0


def test_cost_math_and_tiers():
    c = FakeClock()
    g = CostGuard(CostConfig(daily_budget_usd=10.0), AnthropicConfig(), clock=c)
    assert g.record_usage(1_000_000, 1_000_000) == pytest.approx(6.0)  # $1 in + $5 out
    assert g.sample_rate() == 1.0
    g.record_usage(4_000_000, 0)   # 10.0 total -> ratio 1.0
    assert g.sample_rate() == 0.5
    g.record_usage(5_000_000, 0)   # 15.0 -> 1.5
    assert g.sample_rate() == 0.25
    g.record_usage(5_000_000, 0)   # 20.0 -> 2.0
    assert g.sample_rate() == 0.1


def test_cost_resets_next_day():
    c = FakeClock(86400 * 100 + 10)
    g = CostGuard(CostConfig(daily_budget_usd=1.0), clock=c)
    g.record_usage(5_000_000, 0)
    assert g.sample_rate() < 1.0
    c.advance(86400)
    assert g.sample_rate() == 1.0 and g.spend_usd == 0.0


def test_sampling_deterministic_and_proportional():
    ids = [f"evt-{i}" for i in range(4000)]
    assert sample_bucket("x") == sample_bucket("x")
    g = CostGuard(clock=FakeClock())
    kept = sum(g.should_sample(i, 0.5) for i in ids) / len(ids)
    assert 0.45 < kept < 0.55
    assert all(g.should_sample(i, 1.0) for i in ids)
    # lower of the two rates wins
    g.spend_usd = g.cfg.daily_budget_usd * 2
    assert sum(g.should_sample(i, 0.5) for i in ids) / len(ids) < 0.15


def test_sla_report():
    s = SLATracker()
    for _ in range(99):
        s.record_critical(5)
    s.record_critical(60)
    assert s.report()["critical_path"]["ok"]  # p99 = 5 (nearest rank)
    s.record_critical(60)
    assert not s.report()["critical_path"]["ok"]

    for _ in range(19):
        s.record_enrichment(1.0)
    s.record_enrichment(6.0)
    assert s.report()["enrichment"]["fraction_within"] == 0.95 and s.report()["enrichment"]["ok"]
    s.record_enrichment(6.0)
    assert not s.report()["enrichment"]["ok"]

    s.record_alert(2.5)
    assert s.report()["alert"]["ok"]
    s.record_alert(10.5)
    r = s.report()["alert"]
    assert not r["ok"] and r["breaches"] == 1


def test_queue_leftovers_keep_their_arrival_time():
    """Items left behind by a size flush must still flush max_wait after THEY arrived."""
    c = FakeClock()
    q = MicroBatchQueue(3, 2.0, clock=c)
    for i in range(3):
        q.put(i)
    c.advance(1.5)
    q.put(3)                       # arrives at t=1.5
    assert q.take() == [0, 1, 2]   # size flush
    c.advance(0.6)                 # t=2.1: item 3 waited 0.6s
    assert not q.ready()
    c.advance(1.5)                 # t=3.6: item 3 waited 2.1s -> must be ready
    assert q.ready() and q.take() == [3]
    assert q.first_enqueued_at is None


def test_queue_leftover_older_than_max_wait_is_ready_immediately():
    c = FakeClock()
    q = MicroBatchQueue(2, 2.0, clock=c)
    for i in range(3):
        q.put(i)
    c.advance(2.5)
    assert q.take() == [0, 1]
    assert q.ready()  # item 2 has already waited 2.5s


def test_cost_tiers_order_independent():
    g = CostGuard(CostConfig(daily_budget_usd=1.0, tiers=((1.0, 0.5), (2.0, 0.1), (1.5, 0.25))),
                  clock=FakeClock())
    g.spend_usd = 2.5
    assert g.sample_rate() == 0.1
    g.spend_usd = 1.6
    assert g.sample_rate() == 0.25
