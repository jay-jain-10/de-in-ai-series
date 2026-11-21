"""Circuit breaker: opens when API error rate over a sliding window exceeds 10%.

Thread-safe within one process. In HALF_OPEN exactly one probe call is let through at a time;
everyone else keeps getting the degraded path until the probe reports back (otherwise every
queued request stampedes a recovering API the moment the cooldown ends).
"""

import threading
import time
from collections import deque
from typing import Callable


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, error_threshold: float = 0.10, window: int = 20, min_calls: int = 10,
                 cooldown_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.error_threshold, self.min_calls, self.cooldown = error_threshold, min_calls, cooldown_seconds
        self._results: deque[bool] = deque(maxlen=window)  # True = success
        self._clock = clock
        self._state = self.CLOSED
        self._opened_at = 0.0
        self._probe_started_at: float | None = None
        self._lock = threading.RLock()

    def _current_state(self) -> str:
        if self._state == self.OPEN and self._clock() - self._opened_at >= self.cooldown:
            self._state = self.HALF_OPEN
            self._probe_started_at = None
        return self._state

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def allow(self) -> bool:
        with self._lock:
            st = self._current_state()
            if st == self.CLOSED:
                return True
            if st == self.OPEN:
                return False
            # HALF_OPEN: single probe; a probe that never reported back expires after `cooldown`
            now = self._clock()
            if self._probe_started_at is None or now - self._probe_started_at >= self.cooldown:
                self._probe_started_at = now
                return True
            return False

    def release_probe(self) -> None:
        """The probe ended without a verdict (non-transient error): let another request probe."""
        with self._lock:
            self._probe_started_at = None

    def error_rate(self) -> float:
        with self._lock:
            if not self._results:
                return 0.0
            return 1 - sum(self._results) / len(self._results)

    def record_success(self) -> None:
        with self._lock:
            if self._current_state() == self.HALF_OPEN:   # probe succeeded: API recovered
                self._state = self.CLOSED
                self._results.clear()
                self._probe_started_at = None
                return
            self._results.append(True)

    def record_failure(self) -> None:
        with self._lock:
            if self._current_state() == self.HALF_OPEN:   # probe failed: re-open
                self._trip()
                return
            self._results.append(False)
            if len(self._results) >= self.min_calls and self.error_rate() > self.error_threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = self.OPEN
        self._opened_at = self._clock()
        self._probe_started_at = None
