"""Tiered response cache: L1 Redis (1-day TTL) -> L2 S3 (30-day TTL) -> cold path (API).

Every backend implements get(key) / set(key, value, ttl_seconds) so the gateway
can be handed in-memory fakes in tests and Redis/S3 adapters in production.
"""

import hashlib
import json
import time
from typing import Callable, Optional, Protocol


def cache_key(task: str, model: str, prompt_version: str, payload: dict, scope: str = "") -> str:
    """Hash of the request content, namespaced by `scope` (the authenticated team).

    Pipeline / task_type labels are deliberately excluded so a team's retries and re-runs hit
    the cache. The team IS included: a shared key would let team B learn (via cached=True and
    latency) that team A already submitted a given document - a cross-tenant oracle on claim
    and contract contents."""
    canonical = json.dumps(
        {"scope": scope, "task": task, "model": model, "pv": prompt_version, "payload": payload},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheBackend(Protocol):
    def get(self, key: str) -> Optional[dict]: ...
    def set(self, key: str, value: dict, ttl_seconds: int) -> None: ...


class InMemoryCache:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._data: dict[str, tuple[float, dict]] = {}
        self._clock = clock

    def get(self, key: str) -> Optional[dict]:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            self._data.pop(key, None)   # pop, not del: concurrent readers may race on expiry
            return None
        return value

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._data[key] = (self._clock() + ttl_seconds, value)

    def __len__(self) -> int:
        return len(self._data)


class RedisCache:
    """Adapter over a redis.Redis-like client (get / setex)."""

    def __init__(self, client, prefix: str = "aigw:"):
        self._client, self._prefix = client, prefix

    def get(self, key: str) -> Optional[dict]:
        raw = self._client.get(self._prefix + key)
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._client.setex(self._prefix + key, ttl_seconds, json.dumps(value))


class S3Cache:
    """Adapter over a boto3 S3 client. TTL is stored in the object and checked on read
    (pair with an S3 lifecycle rule for physical deletion)."""

    def __init__(self, client, bucket: str, prefix: str = "gateway-cache/",
                 clock: Callable[[], float] = time.time):
        self._client, self._bucket, self._prefix, self._clock = client, bucket, prefix, clock

    def get(self, key: str) -> Optional[dict]:
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=self._prefix + key)
        except Exception as exc:  # botocore NoSuchKey / ClientError
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404") or type(exc).__name__ == "NoSuchKey":
                return None
            raise
        envelope = json.loads(obj["Body"].read())
        if self._clock() >= envelope["expires_at"]:
            return None
        return envelope["value"]

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        body = json.dumps({"expires_at": self._clock() + ttl_seconds, "value": value})
        self._client.put_object(Bucket=self._bucket, Key=self._prefix + key, Body=body.encode("utf-8"))


class TieredCache:
    def __init__(self, l1: CacheBackend, l2: CacheBackend,
                 l1_ttl: int = 24 * 3600, l2_ttl: int = 30 * 24 * 3600):
        self.l1, self.l2, self.l1_ttl, self.l2_ttl = l1, l2, l1_ttl, l2_ttl

    def get(self, key: str) -> tuple[Optional[dict], Optional[str]]:
        hit = self.l1.get(key)
        if hit is not None:
            return hit, "L1"
        hit = self.l2.get(key)
        if hit is not None:
            self.l1.set(key, hit, self.l1_ttl)  # promote warm -> hot
            return hit, "L2"
        return None, None

    def set(self, key: str, value: dict) -> None:
        self.l1.set(key, value, self.l1_ttl)
        self.l2.set(key, value, self.l2_ttl)
