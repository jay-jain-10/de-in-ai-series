"""Offline demo: fake Kafka + fake Redis + stubbed Claude. No keys needed."""

import json
from unittest.mock import MagicMock

from src.broker import InMemoryBroker
from src.config import PipelineConfig
from src.enrichment_store import InMemoryEnrichmentStore
from src.enricher import AIEnricher
from src.pipeline import StreamingPipeline
from src.state_store import FakeRedis, RedisFeatureStore


def stub_claude():
    client = MagicMock()

    def create(**kw):
        events = json.loads(kw["messages"][0]["content"].split("Events:\n", 1)[1])
        out = [{"event_id": e["event_id"], "risk_score": 90 if e["amount"] > 10000 else 20,
                "anomaly_type": "account_takeover", "confidence": 0.9, "reasoning": "demo"} for e in events]
        r = MagicMock()
        r.content = [MagicMock(text=json.dumps(out))]
        r.usage.input_tokens, r.usage.output_tokens = 500 * len(events), 40 * len(events)
        return r

    client.messages.create.side_effect = create
    return client


if __name__ == "__main__":
    import time
    cfg = PipelineConfig()
    broker = InMemoryBroker()
    for i in range(25):
        broker.produce(cfg.kafka.events_topic, f"u{i % 5}", json.dumps({
            "event_id": f"e{i}", "user_id": f"u{i % 5}", "amount": 15000 if i % 7 == 0 else 50,
            "event_ts": time.time()}))
    p = StreamingPipeline(broker.consumer(cfg.kafka.group_id, [cfg.kafka.events_topic]), broker.producer(),
                          RedisFeatureStore(FakeRedis()), InMemoryEnrichmentStore(),
                          AIEnricher(stub_claude(), cfg.anthropic.model), cfg)
    p.run_once(100)
    p.flush()
    print(json.dumps(p.report(), indent=2, default=str))
