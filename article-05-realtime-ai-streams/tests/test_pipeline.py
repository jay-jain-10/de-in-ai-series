import json
from unittest.mock import MagicMock

from src.alerts import AlertRouter
from src.broker import InMemoryBroker
from src.config import PipelineConfig
from src.enricher import AIEnricher
from src.enrichment_store import InMemoryEnrichmentStore
from src.models import Event, EnrichmentRecord
from src.pipeline import StreamingPipeline
from src.sla import SLATracker
from src.state_store import FakeRedis, RedisFeatureStore
from tests.helpers import FakeClock, make_event, rate_limit_error, scoring_client

T = 1_800_000_000.0


def build(client=None, clock=None, cfg=None, redis=None):
    clock = clock or FakeClock(T)
    cfg = cfg or PipelineConfig()
    broker = InMemoryBroker()
    redis = redis or FakeRedis()
    pipe = StreamingPipeline(
        broker.consumer("g", [cfg.kafka.events_topic]), broker.producer(),
        RedisFeatureStore(redis), InMemoryEnrichmentStore(),
        AIEnricher(client or scoring_client(), cfg.anthropic.model, sleep=lambda s: None),
        cfg, clock=clock)
    return pipe, broker, clock, cfg


def feed(broker, cfg, n, start=0, ts=T, **kw):
    for i in range(start, start + n):
        broker.produce(cfg.kafka.events_topic, f"u{i}", json.dumps(make_event(i, user=f"u{i}", ts=ts, **kw)))


def test_end_to_end_scores_enriches_and_alerts():
    pipe, broker, clock, cfg = build(scoring_client(score=90))
    feed(broker, cfg, 10)
    pipe.run_once()  # 10 events -> exactly one full batch
    assert len(broker.messages(cfg.kafka.scores_topic)) == 10
    assert pipe.enrichment_store.count() == 10
    assert len(broker.messages(cfg.kafka.alerts_topic)) == 10
    assert pipe.consumer.lag() == 0 and broker.offsets  # committed


def test_batch_waits_for_2s_when_under_10_events():
    client = scoring_client()
    pipe, broker, clock, cfg = build(client)
    feed(broker, cfg, 4)
    pipe.run_once()
    assert client.messages.create.call_count == 0 and pipe.enrichment_store.count() == 0
    clock.advance(2.1)
    pipe.run_once()
    assert client.messages.create.call_count == 1 and pipe.enrichment_store.count() == 4


def test_alert_threshold_is_strictly_above_80():
    for score, expected in ((80, 0), (81, 3)):
        pipe, broker, _, cfg = build(scoring_client(score=score))
        feed(broker, cfg, 3)
        pipe.run_once(); pipe.flush()
        assert len(broker.messages(cfg.kafka.alerts_topic)) == expected
        assert pipe.enrichment_store.count() == 3  # always stored


def test_rule_score_survives_total_ai_outage():
    client = MagicMock()
    client.messages.create.side_effect = rate_limit_error()
    pipe, broker, _, cfg = build(client)
    feed(broker, cfg, 10, amount=20_000)
    pipe.run_once()
    scores = [json.loads(m.value) for m in broker.messages(cfg.kafka.scores_topic)]
    assert len(scores) == 10 and all("high_amount" in s["rules_fired"] for s in scores)
    assert pipe.enrichment_store.count() == 0
    assert pipe.metrics.counters["enrichment_failed_events"] == 10


def test_redis_outage_does_not_stop_scoring():
    redis = FakeRedis(); redis.fail = True
    pipe, broker, _, cfg = build(redis=redis)
    feed(broker, cfg, 10)
    pipe.run_once()
    assert pipe.metrics.counters["events_scored"] == 10
    assert pipe.metrics.counters["context_fallbacks"] == 10


def test_malformed_event_goes_to_dlq():
    pipe, broker, _, cfg = build()
    broker.produce(cfg.kafka.events_topic, "k", "{not json")
    broker.produce(cfg.kafka.events_topic, "k2", json.dumps({"event_id": "x"}))
    feed(broker, cfg, 1)
    pipe.run_once()
    assert len(broker.messages(cfg.kafka.dlq_topic)) == 2
    assert pipe.metrics.counters["events_scored"] == 1


def test_replay_is_idempotent_no_double_enrichment_or_double_charge():
    client = scoring_client()
    pipe, broker, _, cfg = build(client)
    feed(broker, cfg, 10)
    pipe.run_once()
    spend, calls = pipe.cost_guard.spend_usd, client.messages.create.call_count
    # simulate Kafka redelivery: same events, new consumer in a fresh group
    broker.offsets.clear()
    pipe.consumer = broker.consumer("g2", [cfg.kafka.events_topic])
    pipe.run_once(); pipe.flush()
    assert pipe.enrichment_store.count() == 10
    assert client.messages.create.call_count == calls
    assert pipe.cost_guard.spend_usd == spend
    assert pipe.metrics.counters["enrichment_skipped_duplicate"] == 10


def test_new_enrichment_version_reenriches():
    cfg = PipelineConfig(); 
    pipe, broker, _, cfg = build(cfg=cfg)
    feed(broker, cfg, 10)
    pipe.run_once()
    pipe.cfg.enrichment_version = 2
    broker.offsets.clear()
    pipe.consumer = broker.consumer("g3", [cfg.kafka.events_topic])
    pipe.run_once()
    assert pipe.enrichment_store.count() == 20


def test_high_lag_widens_batches_and_marks_stale():
    client = scoring_client()
    pipe, broker, clock, cfg = build(client)
    feed(broker, cfg, 5100)
    pipe.run_once(max_messages=100)
    assert pipe.backpressure.level == "high"
    assert pipe.queue.max_events == 50 and pipe.queue.max_wait == 5.0
    # 100 events polled -> 2 batches of 50, none of 10
    sizes = [len(json.loads(c.kwargs["messages"][0]["content"].split("Events:\n", 1)[1]))
             for c in client.messages.create.call_args_list]
    assert sizes == [50, 50]
    assert all(r.stale_context for r in pipe.enrichment_store.rows.values())
    assert pipe.metrics.counters["stale_context_enrichments"] == 100


def test_critical_lag_samples_half():
    pipe, broker, _, cfg = build()
    feed(broker, cfg, 10_300)
    pipe.run_once(max_messages=2000)
    assert pipe.backpressure.level == "critical"
    scored = pipe.metrics.counters["events_scored"]
    sampled_out = pipe.metrics.counters["events_sampled_out"]
    assert scored == 2000 and 0.4 < sampled_out / scored < 0.6
    assert len(broker.messages(cfg.kafka.scores_topic)) == 2000  # critical path unaffected


def test_cost_guardrail_reduces_sampling_during_run():
    cfg = PipelineConfig()
    cfg.cost.daily_budget_usd = 0.01
    pipe, broker, clock, cfg = build(scoring_client(in_tok_per_event=5000), cfg=cfg)
    feed(broker, cfg, 10)
    pipe.run_once()
    assert pipe.cost_guard.sample_rate() < 1.0
    feed(broker, cfg, 1000, start=100)
    before = pipe.enrichment_store.count()
    pipe.run_once(max_messages=1000); pipe.flush()
    enriched_new = pipe.enrichment_store.count() - before
    assert pipe.metrics.counters["events_sampled_out"] > 0
    assert enriched_new < 1000
    assert pipe.report()["sample_rate"] < 1.0


def test_enrichment_sla_and_alert_latency_tracked():
    pipe, broker, clock, cfg = build(scoring_client(score=95))
    feed(broker, cfg, 3, ts=T)
    clock.advance(1.5)
    pipe.run_once(); clock.advance(2.0); pipe.run_once()
    rep = pipe.report()["sla"]
    assert rep["enrichment"]["count"] == 3 and rep["enrichment"]["ok"]
    assert rep["alert"]["count"] == 3 and abs(rep["alert"]["max_seconds"] - 3.5) < 1e-6 and rep["alert"]["ok"]
    assert rep["critical_path"]["ok"]
    # slow enrichment breaches
    feed(broker, cfg, 3, start=50, ts=T)
    clock.advance(20); pipe.run_once(); clock.advance(2.1); pipe.run_once()
    assert not pipe.report()["sla"]["alert"]["ok"]


def test_alert_router_unit():
    broker = InMemoryBroker()
    sla = SLATracker()
    r = AlertRouter(broker.producer(), "alerts", 80, sla, clock=FakeClock(T + 3))
    e = Event(event_id="e", user_id="u", amount=1, event_ts=T)
    rec = EnrichmentRecord(event_id="e", enrichment_version=1, user_id="u", risk_score=81,
                           anomaly_type="account_takeover", confidence=0.9)
    alert = r.route(e, rec)
    assert alert["destination"] == "pagerduty" and sla.alert_s == [3.0]
    rec.risk_score = 80
    assert r.route(e, rec) is None and len(broker.messages("alerts")) == 1


def test_run_demo_importable_and_stub_works():
    import run_demo
    assert run_demo.stub_claude() is not None


def test_duplicate_event_in_same_batch_enriched_and_alerted_once():
    pipe, broker, _, cfg = build(scoring_client(score=95))
    ev = json.dumps(make_event(1, user="u1"))
    broker.produce(cfg.kafka.events_topic, "u1", ev)
    broker.produce(cfg.kafka.events_topic, "u1", ev)  # redelivery lands in the same batch
    pipe.run_once(); pipe.flush()
    assert pipe.enrichment_store.upserts == 1
    assert len(broker.messages(cfg.kafka.alerts_topic)) == 1
    assert pipe.metrics.counters["enrichment_skipped_duplicate"] == 1


def test_unexpected_enricher_error_does_not_kill_the_loop():
    client = MagicMock()
    good = scoring_client()
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            r = MagicMock()
            r.usage = None  # SDK shape change -> AttributeError inside the enricher
            return r
        return good.messages.create(**kw)

    client.messages.create.side_effect = create
    pipe, broker, _, cfg = build(client)
    feed(broker, cfg, 20)
    pipe.run_once()  # two batches of 10: first blows up, second must still run
    assert pipe.metrics.counters["enrichment_errors"] == 1
    assert pipe.metrics.counters["enrichment_failed_events"] == 10
    assert pipe.enrichment_store.count() == 10


def test_replayed_events_do_not_inflate_velocity():
    """Critical path is idempotent: redelivering a user's events must not trip velocity."""
    pipe, broker, _, cfg = build()
    for i in range(3):
        broker.produce(cfg.kafka.events_topic, "u1",
                       json.dumps(make_event(i, user="u1", ts=T + i)))
    pipe.run_once()
    broker.offsets.clear()
    pipe.consumer = broker.consumer("replay", [cfg.kafka.events_topic])
    pipe.run_once()
    scores = [json.loads(m.value) for m in broker.messages(cfg.kafka.scores_topic)]
    assert len(scores) == 6
    assert not any("velocity" in s["rules_fired"] for s in scores)
    ctx, _ = pipe.store.get_context("u1")
    assert ctx["recent_txn_ts"] == [T, T + 1, T + 2]
