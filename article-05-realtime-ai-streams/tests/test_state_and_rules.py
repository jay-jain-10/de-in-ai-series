import json

from src.metrics import Metrics
from src.models import Event, risk_band
from src.resilience import CircuitBreaker
from src.rules import score_event
from src.state_store import FakeRedis, RedisFeatureStore, DEFAULT_CONTEXT
from src.critical_path import CriticalPath
from tests.helpers import FakeClock, make_event

T = 1_800_000_000.0


def ev(**kw):
    d = dict(event_id="e", user_id="u", amount=50, event_ts=T)
    d.update(kw)
    return Event(**d)


def test_risk_bands():
    assert [risk_band(s) for s in (0, 30, 31, 70, 71, 100)] == ["low", "low", "medium", "medium", "high", "high"]


def test_rule_high_amount():
    r = score_event(ev(amount=10_001), DEFAULT_CONTEXT)
    assert "high_amount" in r.rules_fired and r.band == "medium"
    assert score_event(ev(amount=10_000), DEFAULT_CONTEXT).rules_fired == []


def test_rule_foreign_login():
    ctx = dict(DEFAULT_CONTEXT, login_countries_24h=["RU"])
    assert "foreign_login" in score_event(ev(country="US"), ctx).rules_fired
    ctx = dict(DEFAULT_CONTEXT, login_countries_24h=["US"])
    assert score_event(ev(country="US"), ctx).rules_fired == []


def test_rule_velocity_counts_only_last_hour():
    ctx = dict(DEFAULT_CONTEXT, recent_txn_ts=[T - 10, T - 20, T - 30])
    assert "velocity" in score_event(ev(), ctx).rules_fired  # 3 prior + current = 4 > 3
    ctx = dict(DEFAULT_CONTEXT, recent_txn_ts=[T - 7200, T - 7300, T - 10])
    assert "velocity" not in score_event(ev(), ctx).rules_fired


def test_rule_risky_merchant_and_failed_logins():
    r = score_event(ev(merchant_category="crypto", amount=500), dict(DEFAULT_CONTEXT, failed_logins_1h=3))
    assert set(r.rules_fired) == {"risky_merchant_unusual_amount", "failed_logins_then_txn"}
    assert score_event(ev(merchant_category="crypto", amount=200), DEFAULT_CONTEXT).rules_fired == []


def test_score_capped_at_100_and_high_band():
    ctx = dict(DEFAULT_CONTEXT, login_countries_24h=["RU"], recent_txn_ts=[T - 1] * 3, failed_logins_1h=5)
    r = score_event(ev(amount=20_000, merchant_category="crypto"), ctx)
    assert r.score == 100 and r.band == "high"


def test_feature_store_redis_hit_and_new_user():
    redis = FakeRedis()
    store = RedisFeatureStore(redis)
    ctx, src = store.get_context("new")
    assert src == "redis" and ctx["typical_amount"] == 100.0
    redis.data["ctx:u"] = json.dumps({"typical_amount": 900.0})
    ctx, src = store.get_context("u")
    assert src == "redis" and ctx["typical_amount"] == 900.0 and store.fallbacks == 0


def test_feature_store_timeout_falls_back_to_cache_then_defaults():
    redis = FakeRedis()
    store = RedisFeatureStore(redis)
    redis.data["ctx:u"] = json.dumps({"typical_amount": 900.0})
    store.get_context("u")  # warms local cache
    redis.slow = True
    ctx, src = store.get_context("u")
    assert src == "local_cache" and ctx["typical_amount"] == 900.0
    ctx, src = store.get_context("never_seen")
    assert src == "defaults" and ctx == DEFAULT_CONTEXT
    assert store.fallbacks == 2


def test_feature_store_breaker_stops_hammering_redis():
    clock = FakeClock()
    redis = FakeRedis()
    redis.fail = True
    store = RedisFeatureStore(redis, CircuitBreaker(3, 60, clock))
    for _ in range(10):
        store.get_context("u")
    assert redis.calls == 3  # opened after 3 consecutive failures
    clock.advance(61)
    redis.fail = False
    assert store.get_context("u")[1] == "redis"


def test_record_event_keeps_last_ten_and_profile_update_is_idempotent():
    redis = FakeRedis()
    store = RedisFeatureStore(redis)
    ctx, _ = store.get_context("u")
    for i in range(15):
        store.record_event(ev(event_id=f"e{i}", event_ts=T + i, user_id="u"), ctx)
        ctx, _ = store.get_context("u")
    assert len(ctx["recent_txn_ts"]) == 10 and ctx["recent_txn_ts"][-1] == T + 14
    prof = {"risk_score": 90}
    store.update_risk_profile("u", prof)
    snap = redis.data["ctx:u"]
    store.update_risk_profile("u", prof)
    assert redis.data["ctx:u"] == snap


def test_critical_path_fast_and_survives_redis_outage():
    redis = FakeRedis()
    redis.fail = True
    metrics = Metrics()
    cp = CriticalPath(RedisFeatureStore(redis), metrics)
    event, res = cp.process(json.dumps(make_event(amount=20_000)))
    assert res.context_source == "defaults" and "high_amount" in res.rules_fired
    assert res.latency_ms < 50
    assert metrics.counters["context_fallbacks"] == 1


def test_record_event_is_idempotent_per_event_id():
    store = RedisFeatureStore(FakeRedis())
    ctx, _ = store.get_context("u")
    e = ev(event_id="dup", user_id="u")
    store.record_event(e, ctx)
    ctx, _ = store.get_context("u")
    store.record_event(e, ctx)
    ctx, _ = store.get_context("u")
    assert ctx["recent_txn_ts"] == [T] and ctx["recent_txn_ids"] == ["dup"]


def test_velocity_excludes_the_event_itself():
    ctx = dict(DEFAULT_CONTEXT, recent_txn_ts=[T - 10, T - 20, T], recent_txn_ids=["a", "b", "e"])
    assert "velocity" not in score_event(ev(event_id="e"), ctx).rules_fired  # 2 prior + current
    assert "velocity" in score_event(ev(event_id="new"), ctx).rules_fired     # 3 prior + current


def test_older_enrichment_version_never_overwrites_newer_profile():
    redis = FakeRedis()
    store = RedisFeatureStore(redis)
    assert store.update_risk_profile("u", {"risk_score": 90, "version": 2})
    assert not store.update_risk_profile("u", {"risk_score": 10, "version": 1})
    assert json.loads(redis.data["ctx:u"])["ai_risk_profile"]["risk_score"] == 90
    assert store.update_risk_profile("u", {"risk_score": 50, "version": 2})  # same version: replace
