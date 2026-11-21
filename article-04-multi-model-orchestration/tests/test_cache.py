from unittest.mock import MagicMock

from src.cache import InMemoryCache, RedisCache, ReviewCache, content_hash
from tests.conftest import FakeClock


def test_ttl_expiry():
    clock = FakeClock(0)
    c = InMemoryCache(clock)
    c.set("k", "v", 10)
    assert c.get("k") == "v"
    clock.advance(11)
    assert c.get("k") is None


def test_content_hash_ignores_case_and_whitespace_not_id():
    assert content_hash("Great  product") == content_hash("great product ")
    assert content_hash("a") != content_hash("b")


def test_key_namespaced_by_task_and_model():
    k1 = ReviewCache.make_key("sentiment", "haiku", "x")
    assert k1 != ReviewCache.make_key("aspect_extraction", "haiku", "x")
    assert k1 != ReviewCache.make_key("sentiment", "sonnet", "x")


def test_hit_miss_accounting_and_alert():
    rc = ReviewCache(InMemoryCache(), min_hit_rate=0.05, min_samples=10)
    for i in range(20):
        rc.get(f"k{i}")  # all misses
    assert rc.hit_rate == 0.0 and rc.hit_rate_alert()
    rc.set("hot", "1")
    for _ in range(10):
        rc.get("hot")
    assert not rc.hit_rate_alert()
    assert rc.stats()["hits"] == 10


def test_alert_needs_enough_samples():
    rc = ReviewCache(InMemoryCache(), min_samples=100)
    rc.get("a")
    assert not rc.hit_rate_alert()


def test_default_ttl_is_30_days():
    assert ReviewCache(InMemoryCache()).ttl_seconds == 2_592_000


def test_fails_open_when_backend_raises():
    backend = MagicMock()
    backend.get.side_effect = ConnectionError("redis down")
    backend.set.side_effect = ConnectionError("redis down")
    rc = ReviewCache(backend)
    assert rc.get("k") is None
    rc.set("k", "v")  # must not raise
    assert rc.misses == 1


def test_redis_adapter_uses_setex_and_decodes_bytes():
    client = MagicMock()
    client.get.return_value = b'{"a": 1}'
    cache = RedisCache(client)
    assert cache.get("k") == '{"a": 1}'
    cache.set("k", "v", 99)
    client.setex.assert_called_once_with("k", 99, "v")
