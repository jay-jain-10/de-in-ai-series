"""Caching layer: injectable cache interface, Redis implementation, in-memory fake.

Key = content hash of the review text (never review_id: every submission has a unique
id, but true duplicates share content). Key is namespaced by task, model alias and the
concrete model version, so a sentiment result is never served for an aspect request and
a re-run after a model upgrade (e.g. Haiku v1.3 -> v1.4) is not answered from the old
model's cache entries. TTL = 30 days.
"""

import hashlib
import logging
import threading
import time
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class CacheBackend(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class InMemoryCache:
    """In-memory fake with TTL support (injectable clock). Used by tests and local runs."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._data: dict = {}
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires_at = item
            if self._clock() >= expires_at:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        with self._lock:
            self._data[key] = (value, self._clock() + ttl_seconds)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class RedisCache:
    """Thin adapter over a redis-py style client (get / setex). Client is injected."""

    def __init__(self, client):
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisCache":
        import redis  # imported lazily so tests never need a server

        return cls(redis.Redis.from_url(url, decode_responses=True))

    def get(self, key: str) -> Optional[str]:
        value = self._client.get(key)
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._client.setex(key, ttl_seconds, value)


def content_hash(text: str) -> str:
    """Hash of normalized review text (case/whitespace-insensitive)."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ReviewCache:
    """Cache wrapper with hit/miss accounting. Fails open: backend errors are misses."""

    def __init__(
        self,
        backend: CacheBackend,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        min_hit_rate: float = 0.05,
        min_samples: int = 100,
    ):
        self.backend = backend
        self.ttl_seconds = ttl_seconds
        self.min_hit_rate = min_hit_rate
        self.min_samples = min_samples
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def make_key(task: str, model: str, text: str, model_version: str = "") -> str:
        return f"review:{task}:{model}:{model_version}:{content_hash(text)}"

    def get(self, key: str) -> Optional[str]:
        try:
            value = self.backend.get(key)
        except Exception as e:  # cache must never take the pipeline down
            logger.warning("cache get failed: %s", e)
            value = None
        if value is None:
            self.record_miss()
        else:
            with self._lock:
                self.hits += 1
        return value

    def record_miss(self, demote_hit: bool = False) -> None:
        """Count a miss; demote_hit=True converts a hit just counted by get() into a miss
        (used when the cached value turns out to be unreadable)."""
        with self._lock:
            if demote_hit:
                self.hits -= 1
            self.misses += 1

    def set(self, key: str, value: str) -> None:
        try:
            self.backend.set(key, value, self.ttl_seconds)
        except Exception as e:
            logger.warning("cache set failed: %s", e)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def hit_rate_alert(self) -> bool:
        """True when hit rate dropped below 5% (after enough samples): investigate."""
        total = self.hits + self.misses
        return total >= self.min_samples and self.hit_rate < self.min_hit_rate

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "hit_rate": round(self.hit_rate, 4)}
