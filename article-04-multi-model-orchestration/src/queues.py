"""Simple in-memory queues: retry queue, human review queue, dead letter queue.

Stand-ins for SQS / Cloud Tasks. Same interface shape: enqueue and inspect.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class QueueItem:
    review_id: str
    task: str
    reason: str
    payload: dict = field(default_factory=dict)
    visible_at: float = 0.0


class InMemoryQueue:
    def __init__(self, name: str, clock: Callable[[], float] = time.time):
        self.name = name
        self._clock = clock
        self.items: list = []
        self._lock = threading.Lock()

    def enqueue(self, review_id: str, task: str, reason: str, payload=None, delay_s: float = 0) -> None:
        with self._lock:
            self.items.append(
                QueueItem(review_id, task, reason, payload or {}, self._clock() + delay_s)
            )

    def due(self) -> list:
        now = self._clock()
        with self._lock:
            return [i for i in self.items if i.visible_at <= now]

    def __len__(self) -> int:
        return len(self.items)
