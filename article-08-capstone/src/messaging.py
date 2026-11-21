"""Kafka abstraction. The platform depends on `Publisher`, never on a Kafka client."""

import json
from typing import Protocol


class Publisher(Protocol):
    def publish(self, topic: str, key: str, value: dict) -> None: ...


class InMemoryPublisher:
    """Test double / local dev: records messages per topic."""

    def __init__(self):
        self.messages: dict[str, list[tuple[str, dict]]] = {}

    def publish(self, topic: str, key: str, value: dict) -> None:
        self.messages.setdefault(topic, []).append((key, value))

    def values(self, topic: str) -> list[dict]:
        return [v for _, v in self.messages.get(topic, [])]


class KafkaPublisher:
    """Adapter over a confluent_kafka.Producer-like object (produce/flush).

    Construct the producer in deployment code:
        Producer({"bootstrap.servers": cfg.kafka.bootstrap_servers})
    """

    def __init__(self, producer, flush_each: bool = False):
        self._producer = producer
        self._flush_each = flush_each

    def publish(self, topic: str, key: str, value: dict) -> None:
        self._producer.produce(topic, key=key, value=json.dumps(value, default=str).encode("utf-8"))
        if self._flush_each:
            self._producer.flush()

    def flush(self) -> None:
        self._producer.flush()
