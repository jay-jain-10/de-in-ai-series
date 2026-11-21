import json
from unittest.mock import MagicMock

from src.broker import InMemoryBroker
from src.kafka_adapters import ConfluentConsumer, ConfluentProducer


def test_per_key_ordering_and_partitioning():
    b = InMemoryBroker(num_partitions=4)
    for i in range(20):
        b.produce("t", "userA", str(i))
    parts = {m.partition for m in b.messages("t")}
    assert len(parts) == 1
    c = b.consumer("g", ["t"])
    assert [m.value for m in c.poll(100)] == [str(i) for i in range(20)]


def test_lag_and_commit_semantics():
    b = InMemoryBroker()
    for i in range(10):
        b.produce("t", f"k{i}", "x")
    c = b.consumer("g", ["t"])
    assert c.lag() == 10
    c.poll(4)
    assert c.lag() == 6
    # uncommitted: a new consumer in the same group re-reads everything
    assert b.consumer("g", ["t"]).lag() == 10
    c.commit()
    assert b.consumer("g", ["t"]).lag() == 6


def test_separate_groups_independent():
    b = InMemoryBroker()
    b.produce("t", "k", "x")
    c1, c2 = b.consumer("g1", ["t"]), b.consumer("g2", ["t"])
    c1.poll(10); c1.commit()
    assert c2.lag() == 1


def test_poll_respects_max_messages():
    b = InMemoryBroker()
    for i in range(10):
        b.produce("t", f"k{i}", "x")
    assert len(b.consumer("g", ["t"]).poll(3)) == 3


def test_confluent_adapters_with_mock_clients():
    prod_client = MagicMock()
    ConfluentProducer(prod_client).produce("topic", "k", "v")
    prod_client.produce.assert_called_once_with("topic", key="k", value="v")

    raw = MagicMock()
    raw.error.return_value = None
    raw.topic.return_value, raw.partition.return_value, raw.offset.return_value = "t", 2, 7
    raw.key.return_value, raw.value.return_value = b"k", b'{"a":1}'
    cons_client = MagicMock()
    cons_client.consume.return_value = [raw]
    c = ConfluentConsumer(cons_client, ["t"], lag_fn=lambda: 42)
    cons_client.subscribe.assert_called_once_with(["t"])
    msgs = c.poll(10)
    assert msgs[0].offset == 7 and json.loads(msgs[0].value) == {"a": 1}
    c.commit()
    cons_client.commit.assert_called_once_with(asynchronous=False)
    assert c.lag() == 42


def test_confluent_consumer_skips_and_logs_errors_and_tombstones(caplog):
    def msg(err=None, value=b"{}", offset=0):
        m = MagicMock()
        m.error.return_value = err
        m.topic.return_value, m.partition.return_value, m.offset.return_value = "t", 0, offset
        m.key.return_value, m.value.return_value = b"k", value
        return m

    eof, real = MagicMock(), MagicMock()
    eof.code.return_value, real.code.return_value = -191, 7
    client = MagicMock()
    client.consume.return_value = [msg(err=eof), msg(err=real), msg(value=None), msg(offset=9)]
    with caplog.at_level("WARNING"):
        out = ConfluentConsumer(client, ["t"]).poll(10)
    assert [m.offset for m in out] == [9]
    assert sum("consume error" in r.message for r in caplog.records) == 1  # EOF not logged
    assert any("null-value" in r.message for r in caplog.records)
