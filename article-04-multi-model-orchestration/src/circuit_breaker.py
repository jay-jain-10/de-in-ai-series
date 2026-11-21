"""Per-model circuit breaker with graceful degradation.

- Opens after 5 consecutive failures.
- After 60s, moves to half-open and tests recovery with a small slice of traffic (1%),
  then ramps 1% -> 10% -> 50% -> 100% on successes. Any failure re-opens it.
- Tracks health: consecutive failures, last success, state, fallback rate (alert > 5%).
"""

import random
import threading
import time
from typing import Callable, Optional

from .config import BreakerSettings

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        settings: Optional[BreakerSettings] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ):
        s = settings or BreakerSettings()
        self.name = name
        self.failure_threshold = s.failure_threshold
        self.recovery_timeout_s = s.recovery_timeout_s
        self.ramp_steps = tuple(s.ramp_steps)
        self.fallback_rate_alert = s.fallback_rate_alert
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        self.state = CLOSED
        self.consecutive_failures = 0
        self.last_success_at: Optional[float] = None
        self._opened_at = 0.0
        self._last_probe_at = 0.0
        self._ramp_idx = 0
        self.requests = 0
        self.fallbacks = 0

    def allow_request(self) -> bool:
        with self._lock:
            self.requests += 1
            if self.state == CLOSED:
                return True
            now = self._clock()
            if self.state == OPEN:
                if now - self._opened_at < self.recovery_timeout_s:
                    return False
                self.state = HALF_OPEN
                self._ramp_idx = 0
                self._last_probe_at = self._opened_at
            # HALF_OPEN: admit ramp fraction of traffic, plus a guaranteed probe every 60s
            fraction = self.ramp_steps[self._ramp_idx]
            if self._rng() < fraction or now - self._last_probe_at >= self.recovery_timeout_s:
                self._last_probe_at = now
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self.last_success_at = self._clock()
            self.consecutive_failures = 0
            if self.state == HALF_OPEN:
                self._ramp_idx += 1
                if self._ramp_idx >= len(self.ramp_steps):
                    self.state = CLOSED
                    self._ramp_idx = 0

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.state == HALF_OPEN or (
                self.state == CLOSED and self.consecutive_failures >= self.failure_threshold
            ):
                self.state = OPEN
                self._opened_at = self._clock()

    def record_fallback(self) -> None:
        with self._lock:
            self.fallbacks += 1

    @property
    def fallback_rate(self) -> float:
        with self._lock:
            return self.fallbacks / self.requests if self.requests else 0.0

    def health(self) -> dict:
        rate = self.fallback_rate
        with self._lock:
            return self._health_locked(rate)

    def _health_locked(self, rate: float) -> dict:
        return {
            "model": self.name,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "seconds_since_last_success": (
                None if self.last_success_at is None else self._clock() - self.last_success_at
            ),
            "requests": self.requests,
            "fallbacks": self.fallbacks,
            "fallback_rate": round(rate, 4),
        }


class BreakerRegistry:
    """One breaker per model alias's underlying model."""

    def __init__(self, settings: Optional[BreakerSettings] = None, clock=time.monotonic,
                 rng=random.random):
        self.settings = settings or BreakerSettings()
        self._clock = clock
        self._rng = rng
        self._breakers: dict = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, self.settings, self._clock, self._rng)
            return self._breakers[name]

    def _snapshot(self) -> list:
        # copy under the lock: get() may insert concurrently from worker threads
        with self._lock:
            return list(self._breakers.values())

    def alerts(self) -> list:
        """Models whose fallback rate exceeds the alert threshold (default 5%)."""
        return [
            b.name for b in self._snapshot()
            if b.fallback_rate > self.settings.fallback_rate_alert
        ]

    def health(self) -> list:
        return [b.health() for b in self._snapshot()]
