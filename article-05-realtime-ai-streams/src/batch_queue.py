"""Micro-batching queue: flush every N events or T seconds, whichever first.

Bounded: when full beyond `max_pending`, new items are rejected (shed) rather
than growing without limit - the caller counts these as dropped enrichments.
"""

import time


class MicroBatchQueue:
    def __init__(self, max_events: int = 10, max_wait_seconds: float = 2.0,
                 max_pending: int = 10_000, clock=time.time):
        self.max_events = max_events
        self.max_wait = max_wait_seconds
        self.max_pending = max_pending
        self.clock = clock
        self.items = []
        self._enqueued_at = []  # parallel to items: when each item arrived
        self.dropped = 0

    @property
    def first_enqueued_at(self):
        """Arrival time of the oldest pending item (None when empty)."""
        return self._enqueued_at[0] if self._enqueued_at else None

    def __len__(self):
        return len(self.items)

    def configure(self, max_events: int, max_wait_seconds: float):
        self.max_events, self.max_wait = max_events, max_wait_seconds

    def put(self, item) -> bool:
        if len(self.items) >= self.max_pending:
            self.dropped += 1
            return False
        self.items.append(item)
        self._enqueued_at.append(self.clock())
        return True

    def ready(self) -> bool:
        if not self.items:
            return False
        if len(self.items) >= self.max_events:
            return True
        return (self.clock() - self.first_enqueued_at) >= self.max_wait

    def take(self, force: bool = False) -> list:
        """Pop one batch (up to max_events) if ready (or forced)."""
        if not self.items or not (force or self.ready()):
            return []
        n = self.max_events
        batch, self.items = self.items[:n], self.items[n:]
        # Leftover items keep their real arrival time, so the max-wait bound still holds
        # for them (resetting the timer here would let them wait up to 2x max_wait).
        self._enqueued_at = self._enqueued_at[n:]
        return batch
