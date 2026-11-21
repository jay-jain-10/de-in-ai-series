import io
import json
from unittest.mock import MagicMock

import pytest

from src.gateway.cache import InMemoryCache, RedisCache, S3Cache, TieredCache, cache_key
from src.gateway.circuit_breaker import CircuitBreaker


def test_cache_key_is_deterministic_and_content_sensitive():
    a = cache_key("extract", "m", "1.0", {"x": 1, "y": 2})
    assert a == cache_key("extract", "m", "1.0", {"y": 2, "x": 1})
    assert a != cache_key("extract", "m", "1.1", {"x": 1, "y": 2})
    assert a != cache_key("classify", "m", "1.0", {"x": 1, "y": 2})


def test_in_memory_ttl():
    t = {"now": 0}
    c = InMemoryCache(lambda: t["now"])
    c.set("k", {"v": 1}, 10)
    assert c.get("k") == {"v": 1}
    t["now"] = 10
    assert c.get("k") is None


def test_redis_adapter_uses_setex_and_json():
    client = MagicMock()
    c = RedisCache(client)
    c.set("k", {"a": 1}, 86400)
    client.setex.assert_called_once_with("aigw:k", 86400, json.dumps({"a": 1}))
    client.get.return_value = json.dumps({"a": 1})
    assert c.get("k") == {"a": 1}
    client.get.return_value = None
    assert c.get("missing") is None


class FakeS3:
    """Minimal boto3 S3 client double."""

    class NoSuchKey(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    def __init__(self):
        self.objects = {}
        self.put_object = MagicMock(side_effect=self._put)
        self.get_object = MagicMock(side_effect=self._get)

    def _put(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def _get(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def test_s3_adapter_roundtrip_ttl_and_miss():
    t = {"now": 100.0}
    s3 = FakeS3()
    c = S3Cache(s3, "bkt", clock=lambda: t["now"])
    assert c.get("k") is None
    c.set("k", {"r": 1}, 30 * 24 * 3600)
    assert s3.put_object.call_args.kwargs["Bucket"] == "bkt"
    assert c.get("k") == {"r": 1}
    t["now"] += 31 * 24 * 3600
    assert c.get("k") is None


def test_s3_adapter_reraises_unexpected_errors():
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        S3Cache(s3, "b").get("k")


def test_tiered_cache_ttls_and_promotion():
    l1, l2 = MagicMock(), MagicMock()
    l1.get.return_value = None
    l2.get.return_value = {"x": 1}
    tc = TieredCache(l1, l2, l1_ttl=86400, l2_ttl=30 * 86400)
    assert tc.get("k") == ({"x": 1}, "L2")
    l1.set.assert_called_with("k", {"x": 1}, 86400)      # promoted into hot cache
    tc.set("k2", {"y": 2})
    l1.set.assert_called_with("k2", {"y": 2}, 86400)      # Redis: 1 day
    l2.set.assert_called_with("k2", {"y": 2}, 30 * 86400)  # S3: 30 days
    l1.get.return_value = None
    l2.get.return_value = None
    assert tc.get("nope") == (None, None)


class Clk:
    t = 0.0

    def __call__(self):
        return self.t


def test_breaker_requires_min_calls_and_threshold():
    cb = CircuitBreaker(error_threshold=0.10, window=20, min_calls=10, clock=Clk())
    for _ in range(9):
        cb.record_failure()
    assert cb.state == "closed"        # not enough calls yet
    cb.record_failure()
    assert cb.state == "open" and not cb.allow()


def test_breaker_tolerates_error_rate_at_or_below_10_percent():
    cb = CircuitBreaker(error_threshold=0.10, window=20, min_calls=10, clock=Clk())
    for _ in range(18):
        cb.record_success()
    cb.record_failure()
    cb.record_failure()                # 2/20 = 10% -> not "> 10%"
    assert cb.state == "closed"
    cb.record_failure()                # window slides: 3/20 = 15%
    assert cb.state == "open"


def test_breaker_half_open_probe_success_closes_failure_reopens():
    clk = Clk()
    cb = CircuitBreaker(min_calls=2, cooldown_seconds=30, clock=clk)
    cb.record_failure(); cb.record_failure()
    assert cb.state == "open"
    clk.t = 29
    assert cb.state == "open"
    clk.t = 31
    assert cb.state == "half_open" and cb.allow()
    cb.record_failure()
    assert cb.state == "open"
    clk.t = 70
    assert cb.state == "half_open"
    cb.record_success()
    assert cb.state == "closed" and cb.error_rate() == 0.0
