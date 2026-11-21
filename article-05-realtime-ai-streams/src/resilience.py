"""Circuit breaker and backoff helpers (clock/sleep injectable for tests)."""

import random
import time


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures for `open_seconds`.

    Article: "if three API calls fail in a row, stop sending requests for sixty seconds."
    """

    def __init__(self, failure_threshold: int = 3, open_seconds: float = 60.0, clock=time.time):
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.clock = clock
        self.consecutive_failures = 0
        self.opened_at = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self.clock() - self.opened_at >= self.open_seconds:
            # half-open: allow a trial call
            self.opened_at = None
            self.consecutive_failures = self.failure_threshold - 1
            return True
        return False

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and (self.clock() - self.opened_at) < self.open_seconds

    def record_success(self):
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self.clock()


def backoff_delay(attempt: int, base: float = 0.1, cap: float = 30.0, rng=random.random) -> float:
    """Exponential backoff with full jitter: uniform(0, min(cap, base * 2^attempt))."""
    return rng() * min(cap, base * (2 ** attempt))
