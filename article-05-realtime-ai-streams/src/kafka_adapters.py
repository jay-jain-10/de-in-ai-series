"""Thin adapters from confluent-kafka clients to the Consumer/Producer protocols.

The underlying client is injected so unit tests can pass a mock. Never run
against a real cluster in this repo's tests.
"""

import logging
from typing import Optional
from src.broker import Message

logger = logging.getLogger(__name__)
_PARTITION_EOF = -191  # confluent_kafka.KafkaError._PARTITION_EOF: informational


class ConfluentProducer:
    def __init__(self, client):
        self.client = client  # confluent_kafka.Producer

    def produce(self, topic: str, key: Optional[str], value: str) -> None:
        self.client.produce(topic, key=key, value=value)
        self.client.poll(0)

    def flush(self, timeout: float = 5.0):
        self.client.flush(timeout)


class ConfluentConsumer:
    def __init__(self, client, topics: list, lag_fn=None):
        self.client = client  # confluent_kafka.Consumer (enable.auto.commit=false)
        self.client.subscribe(topics)
        self._lag_fn = lag_fn
        self._lag = 0

    def poll(self, max_messages: int) -> list:
        raw = self.client.consume(num_messages=max_messages, timeout=0.01) or []
        out = []
        for m in raw:
            err = m.error()
            if err:
                if getattr(err, "code", lambda: None)() != _PARTITION_EOF:
                    logger.error("kafka consume error on %s[%s]: %s", m.topic(), m.partition(), err)
                continue
            if m.value() is None:  # tombstone: nothing to score
                logger.warning("skipping null-value message %s[%s]@%s", m.topic(), m.partition(), m.offset())
                continue
            key = m.key().decode() if m.key() else None
            out.append(Message(m.topic(), m.partition(), m.offset(), key, m.value().decode()))
        return out

    def commit(self) -> None:
        self.client.commit(asynchronous=False)

    def lag(self) -> int:
        # Real lag = sum(high watermark - committed position) per partition,
        # supplied by the caller (AdminClient / get_watermark_offsets).
        return self._lag_fn() if self._lag_fn else self._lag
