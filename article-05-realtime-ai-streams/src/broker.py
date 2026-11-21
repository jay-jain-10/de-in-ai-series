"""Kafka abstractions.

Pipeline code depends only on the Consumer / Producer protocols below. Tests
inject InMemoryBroker; production wires the thin confluent-kafka adapters in
kafka_adapters.py.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Message:
    topic: str
    partition: int
    offset: int
    key: Optional[str]
    value: str  # JSON string


class Producer(Protocol):
    def produce(self, topic: str, key: Optional[str], value: str) -> None: ...


class Consumer(Protocol):
    def poll(self, max_messages: int) -> list: ...
    def commit(self) -> None: ...
    def lag(self) -> int: ...


class InMemoryBroker:
    """Fake Kafka: partitioned append-only logs with consumer-group offsets.

    Keys hash to partitions, so all events for one user stay ordered.
    """

    def __init__(self, num_partitions: int = 3):
        self.num_partitions = num_partitions
        self.logs = {}      # topic -> list[list[Message]]
        self.offsets = {}   # (group, topic, partition) -> committed offset

    def _log(self, topic):
        return self.logs.setdefault(topic, [[] for _ in range(self.num_partitions)])

    def _partition(self, key):
        if key is None:
            return 0
        return int(hashlib.md5(key.encode()).hexdigest(), 16) % self.num_partitions

    def produce(self, topic, key, value):
        p = self._partition(key)
        log = self._log(topic)[p]
        log.append(Message(topic, p, len(log), key, value))

    def messages(self, topic):
        """All messages on a topic (test helper)."""
        return [m for part in self._log(topic) for m in part]

    def producer(self) -> "InMemoryProducer":
        return InMemoryProducer(self)

    def consumer(self, group: str, topics: list) -> "InMemoryConsumer":
        return InMemoryConsumer(self, group, topics)


class InMemoryProducer:
    def __init__(self, broker: InMemoryBroker):
        self.broker = broker

    def produce(self, topic, key, value):
        self.broker.produce(topic, key, value)


class InMemoryConsumer:
    """Consumer with position (in-flight) vs committed offsets, like Kafka."""

    def __init__(self, broker: InMemoryBroker, group: str, topics: list):
        self.broker, self.group, self.topics = broker, group, topics
        self.position = {}
        for t in topics:
            for p in range(broker.num_partitions):
                self.position[(t, p)] = broker.offsets.get((group, t, p), 0)

    def poll(self, max_messages: int) -> list:
        out = []
        progressed = True
        while len(out) < max_messages and progressed:
            progressed = False
            for (t, p), pos in sorted(self.position.items()):
                log = self.broker._log(t)[p]
                if pos < len(log) and len(out) < max_messages:
                    out.append(log[pos])
                    self.position[(t, p)] = pos + 1
                    progressed = True
        return out

    def commit(self):
        for (t, p), pos in self.position.items():
            self.broker.offsets[(self.group, t, p)] = pos

    def lag(self) -> int:
        """Events produced but not yet polled by this consumer."""
        total = 0
        for (t, p), pos in self.position.items():
            total += len(self.broker._log(t)[p]) - pos
        return total
