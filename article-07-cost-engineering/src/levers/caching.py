"""Lever 1: Caching (article: 40-60% reduction).

- Content-hash caching: SHA256 of normalized text (lowercase, trimmed), 30-day TTL.
- Semantic dedup: cosine similarity > 0.95 against cached embeddings reuses the
  cached response. The embedder is injected (article: lightweight local model).
"""

import hashlib
import json
import math
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

TTL_SECONDS = 30 * 24 * 3600
SIMILARITY_THRESHOLD = 0.95


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryCacheBackend:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._d: Dict[str, Tuple[float, str]] = {}
        self._clock = clock

    def get(self, key: str, ttl: int = TTL_SECONDS) -> Optional[str]:
        item = self._d.get(key)
        if not item:
            return None
        stored_at, value = item
        if self._clock() - stored_at > ttl:
            del self._d[key]
            return None
        return value

    def put(self, key: str, value: str) -> None:
        self._d[key] = (self._clock(), value)


class S3CacheBackend:
    """S3 cache (Intelligent-Tiering). boto3 client injected; TTL via LastModified."""

    def __init__(self, bucket: str, client=None, prefix: str = "cache/",
                 clock: Callable[[], float] = time.time):
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.client, self.bucket, self.prefix, self._clock = client, bucket, prefix, clock

    def get(self, key: str, ttl: int = TTL_SECONDS) -> Optional[str]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)
        except Exception as e:  # NoSuchKey etc.
            if "NoSuchKey" in type(e).__name__ or "NoSuchKey" in str(e):
                return None
            raise
        if self._clock() - obj["LastModified"].timestamp() > ttl:
            return None
        return obj["Body"].read().decode("utf-8")

    def put(self, key: str, value: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key,
                               Body=value.encode("utf-8"),
                               StorageClass="INTELLIGENT_TIERING")


class ResponseCache:
    def __init__(self, backend=None, embedder: Optional[Callable[[str], List[float]]] = None,
                 threshold: float = SIMILARITY_THRESHOLD, ttl: int = TTL_SECONDS):
        self.backend = backend or InMemoryCacheBackend()
        self.embedder = embedder
        self.threshold = threshold
        self.ttl = ttl
        self._embeddings: List[Tuple[List[float], str]] = []  # (vector, hash key)
        self.hits = 0
        self.misses = 0

    def get(self, text: str) -> Optional[Tuple[str, str]]:
        """Return (response, kind) with kind in {'exact','semantic'} or None."""
        key = content_hash(text)
        hit = self.backend.get(key, self.ttl)
        if hit is not None:
            self.hits += 1
            return hit, "exact"
        if self.embedder and self._embeddings:
            vec = self.embedder(text)
            best_sim, best_key = 0.0, None
            for v, k in self._embeddings:
                s = cosine(vec, v)
                if s > best_sim:
                    best_sim, best_key = s, k
            if best_key and best_sim > self.threshold:
                val = self.backend.get(best_key, self.ttl)
                if val is not None:
                    self.hits += 1
                    return val, "semantic"
        self.misses += 1
        return None

    def put(self, text: str, response: str) -> None:
        key = content_hash(text)
        self.backend.put(key, response)
        if self.embedder:
            self._embeddings.append((self.embedder(text), key))

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
