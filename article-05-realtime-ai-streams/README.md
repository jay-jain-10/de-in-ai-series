# Article 5: Where AI Meets Event-Driven Architecture

**A hybrid streaming + AI architecture for fintech fraud detection**

Part 5 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

Language model inference (200ms to 2s, variable) breaks the bounded-latency assumptions of streaming. This project splits the work in two:

- **Critical path** (target <50ms p99): parse, Redis context lookup (100ms bound with fallbacks), deterministic rule-based score. No AI, no external dependency beyond Redis.
- **Async enrichment path**: sampled events go into a micro-batch queue (10 events or 2s), one Claude Haiku call per batch, idempotent upsert on `(event_id, enrichment_version)`, Redis profile write-back, and alert routing for AI score > 80.

Around those sit backpressure handling and lag metrics, a cost guardrail, and tracking for the three SLAs.

## Architecture

```
                      +--------------------- CRITICAL PATH (<50ms p99) ----------------------+
                      |  parse --> Redis context --> rule score (0-30 low/31-70 med/71-100)  |
 Kafka  --poll/commit-+     (100ms timeout -> local cache -> defaults, circuit breaker)       +--> risk-scores topic
 topic                |                                                                       |
                      +---------------------------------+-------------------------------------+
                                                        | sampled? (backpressure x cost guard)
                                                        v
                      +------------------ ASYNC ENRICHMENT PATH (0.2-2.2s) ------------------+
                      |  MicroBatchQueue        AIEnricher              Persist & react       |
                      |  10 evts / 2s   -->  Claude Haiku (1 call  -->  upsert (event_id,     |
                      |  (50 / 5s under      per batch, retry+          enrichment_version)   |
                      |   backpressure)      jitter, breaker:           Redis profile SET     |
                      |                      3 fails -> 60s open)       AlertRouter           |
                      +----------------------------------------------------+------------------+
                                                                           |
                                                          score > 80 ? ----+--> fraud-alerts topic (PagerDuty)
                                                          else         -----> store only

 Controls:  BackpressureController  lag > 5000 -> wide batches; lag > 10000 -> sample 50%; lag > 5s -> stale flag
            CostGuard               daily spend vs budget -> lower sample rate (deterministic by event_id)
            SLATracker              critical <50ms p99 | enrichment <5s for 95% | alert <10s
```

## Tech Stack

| Component | Tool |
|-----------|------|
| AI Model | Claude Haiku (claude-haiku-4-5-20251001), same id/config convention as Article 1 |
| Event stream | Kafka (AWS MSK) via `confluent-kafka` adapters |
| State / feature store | Redis (ElastiCache) via `redis-py` client |
| Persistence | PostgreSQL (RDS) UPSERT via any DB-API connection |
| Validation | Pydantic v2 |
| Language | Python 3.10+ |

## Project Structure

```
article-05-realtime-ai-streams/
├── src/
│   ├── config.py            # Config dataclasses (article defaults)
│   ├── models.py            # Event, RuleScore, Enrichment, EnrichmentRecord
│   ├── broker.py            # Consumer/Producer protocols + InMemoryBroker (fake Kafka)
│   ├── kafka_adapters.py    # confluent-kafka adapters (client injected)
│   ├── state_store.py       # RedisFeatureStore + FakeRedis (timeout/fallback/breaker)
│   ├── rules.py             # 5 rule-based fraud rules, score bands
│   ├── critical_path.py     # parse -> context -> score, latency measured
│   ├── batch_queue.py       # MicroBatchQueue (size/time flush, bounded)
│   ├── enricher.py          # Claude batch enrichment, retries, circuit breaker
│   ├── enrichment_store.py  # Idempotent store: in-memory fake + Postgres UPSERT
│   ├── backpressure.py      # Lag-driven batch widening / sampling / staleness
│   ├── cost_guard.py        # Daily spend tracking + sample-rate tiers
│   ├── alerts.py            # Score > 80 -> alerts topic
│   ├── sla.py               # Three SLA trackers
│   ├── resilience.py        # CircuitBreaker, backoff with jitter
│   ├── metrics.py           # Counters, gauges, percentiles
│   └── pipeline.py          # StreamingPipeline orchestrator
├── tests/                   # 67 offline tests
├── run_demo.py              # Offline demo (fake Kafka/Redis, stubbed Claude)
├── pytest.ini
├── .env.example
├── requirements.txt
└── README.md
```

## How to Run Tests

```bash
pip install -r requirements.txt   # tests need: anthropic, pydantic, python-dotenv, pytest
python3 -m pytest -q
python3 run_demo.py               # prints metrics + SLA report, no keys required
```

All tests run offline with no API keys, no network.

## Key Concepts

- **Critical path stays deterministic**: rule scores are always shipped; AI is additive, so an AI outage never loses detection.
- **Idempotency**: enrichments are keyed on `(event_id, enrichment_version)`. Replayed events are skipped before the Claude call (no double cost); retried upserts leave one row.
- **Redis as a real-time dimension join**: bounded lookup, local-cache fallback, defaults, breaker. The context source is recorded so fallbacks are visible.
- **Backpressure as a quality signal**: enrichments produced while lag > 5s (or lag high) carry `stale_context=true`.
- **Cost as a runtime constraint**: spend above budget lowers the sampling rate (budget 100% -> 50%, 150% -> 25%, 200% -> 10%).

## What Is Mocked / Not Tested Against Live Services

Nothing in this repo has been run against a real Anthropic API, Kafka cluster, Redis, or PostgreSQL.

- **Anthropic**: `MagicMock` clients return canned JSON. Real model output quality, real token counts, latency, and rate-limit behavior are untested. The prompt has not been evaluated for accuracy. Error classes are real SDK classes constructed locally.
- **Kafka**: `InMemoryBroker` mimics partitions, key hashing, offsets and lag. It does not model rebalances, consumer-group coordination, exactly-once transactions, or broker failures. `kafka_adapters.py` is tested only against mock clients and does not compute real lag (a `lag_fn` must be supplied).
- **Redis**: `FakeRedis` supports `get`/`set` and simulated timeout/outage. Real `redis-py` exceptions, the 100ms socket timeout, replicas and failover are not exercised. The fake does not enforce time-based timeouts; "timeout" is injected as an exception.
- **PostgreSQL**: the UPSERT is exercised against SQLite (same `ON CONFLICT` syntax, `?` placeholders), not Postgres. `psycopg2` paths are untested.
- **Performance**: tests assert the SLA *logic*, not that the 8ms / <50ms p99 latency holds under 1000 events/sec. The pipeline is single-threaded and synchronous; concurrency, worker pools and real throughput are not modeled.
- **Cost numbers**: the AWS/GCP cost figures in the article are not computed by this code. Per-call spend uses the Haiku prices in `config.py` ($1/$5 per 1M tokens, as in Article 1), which differ from the $0.80 input price quoted in the article.
- **PagerDuty / SNS**: alerts are published to a Kafka topic; no downstream delivery is implemented.
