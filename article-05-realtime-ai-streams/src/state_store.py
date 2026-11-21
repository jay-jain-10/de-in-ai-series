"""Redis as a real-time dimension join: hot user context for scoring.

Bounded lookup (100ms) -> local in-memory cache -> defaults. Freshness is
traded for reliability, and the source of the context is reported so
downstream can see degradation (fallback rate metric).
"""

import json
import time
from typing import Optional

from src.models import Event
from src.resilience import CircuitBreaker

DEFAULT_CONTEXT = {
    "typical_amount": 100.0,
    "recent_txn_ts": [],       # epoch seconds of recent transactions
    "recent_txn_ids": [],      # event_ids parallel to recent_txn_ts (replay de-dup)
    "login_countries_24h": [],  # countries seen in last 24h logins
    "failed_logins_1h": 0,
    "ai_risk_profile": None,   # written back by the enrichment path
}
MAX_HISTORY = 10  # "user's last ten transactions"


def align_ids(ts: list, ids: list) -> list:
    """Pad/trim event ids to line up with recent_txn_ts (legacy rows have no ids)."""
    if not ts:
        return []
    return ([None] * (len(ts) - len(ids)) + list(ids))[-len(ts):]


class FakeRedis:
    """In-memory stand-in with the subset of redis-py used here."""

    def __init__(self):
        self.data = {}
        self.fail = False      # raise ConnectionError
        self.slow = False      # raise TimeoutError (simulates > timeout)
        self.calls = 0

    def _check(self):
        self.calls += 1
        if self.slow:
            raise TimeoutError("redis timeout")
        if self.fail:
            raise ConnectionError("redis down")

    def get(self, key):
        self._check()
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self._check()
        self.data[key] = value  # SET overwrites: naturally idempotent
        return True


class RedisFeatureStore:
    def __init__(self, client, breaker: Optional[CircuitBreaker] = None, key_prefix: str = "ctx:"):
        self.client = client  # redis.Redis(socket_timeout=0.1) in production
        self.prefix = key_prefix
        self.breaker = breaker or CircuitBreaker(clock=time.time)
        self.local_cache = {}
        self.fallbacks = 0
        self.lookups = 0

    def get_context(self, user_id: str):
        """Returns (context dict, source) where source is redis|local_cache|defaults."""
        self.lookups += 1
        if self.breaker.allow():
            try:
                raw = self.client.get(self.prefix + user_id)
                self.breaker.record_success()
                ctx = dict(DEFAULT_CONTEXT)
                if raw is not None:
                    ctx.update(json.loads(raw))
                    self.local_cache[user_id] = ctx
                    return ctx, "redis"
                # key absent is a valid answer (new user): not a fallback
                return ctx, "redis"
            except Exception:
                self.breaker.record_failure()
        self.fallbacks += 1
        if user_id in self.local_cache:
            return dict(self.local_cache[user_id]), "local_cache"
        return dict(DEFAULT_CONTEXT), "defaults"

    def _put(self, user_id: str, ctx: dict) -> bool:
        self.local_cache[user_id] = ctx
        if not self.breaker.allow():
            return False
        try:
            self.client.set(self.prefix + user_id, json.dumps(ctx))
            self.breaker.record_success()
            return True
        except Exception:
            self.breaker.record_failure()
            return False

    def record_event(self, event: Event, ctx: dict) -> bool:
        """Append the transaction to the user's hot history (best effort).

        Idempotent per event_id: a Kafka redelivery must not append the same transaction
        twice, or the velocity rule would fire on replays.
        """
        ctx = dict(ctx)
        ts = list(ctx.get("recent_txn_ts", []))
        ids = list(ctx.get("recent_txn_ids", []))
        ids = align_ids(ts, ids)
        if event.event_id in ids:
            return True
        ctx["recent_txn_ts"] = (ts + [event.event_ts])[-MAX_HISTORY:]
        ctx["recent_txn_ids"] = (ids + [event.event_id])[-MAX_HISTORY:]
        ctx["login_countries_24h"] = list(ctx.get("login_countries_24h", []))
        return self._put(event.user_id, ctx)

    def update_risk_profile(self, user_id: str, profile: dict) -> bool:
        """Enrichment write-back. SET semantics: replaying is harmless.

        A profile from an older enrichment_version never overwrites a newer one.
        """
        ctx, _ = self.get_context(user_id)
        current = ctx.get("ai_risk_profile") or {}
        if (current.get("version") is not None and profile.get("version") is not None
                and profile["version"] < current["version"]):
            return False
        ctx["ai_risk_profile"] = profile
        return self._put(user_id, ctx)
