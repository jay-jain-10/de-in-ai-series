# Article 4: Designing for Model Heterogeneity

**When One Model Isn't Enough: Multi-Model Orchestration for 100K Reviews/Day**

Part 4 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

Runnable companion code for an e-commerce review pipeline (sentiment, aspect extraction,
toxicity detection) that routes work across Claude Haiku, Sonnet and Opus. It implements:

- The four orchestration patterns: **Router**, **Chain**, **Fan-Out/Fan-In**, **Fallback Cascade**
- YAML-driven model routing (complexity score, embedding similarity, keyword urgency)
- Redis caching behind an injectable interface (content-hash key namespaced by task, model and model version; 30-day TTL, hit-rate alert)
- Per-model circuit breaker (5 failures to open, 60s recovery probe, 1%/10%/50%/100% ramp)
- Cost-tracking middleware (tokens, cost, latency, routing decision, cache hit per call)
- Idempotent results keyed by `(review_id, model_version, task_type)` with MERGE semantics
- Dagster asset graph with a model-aware I/O manager and p50/p95/p99 latency measurement

## Architecture

```
                         +--------------------------- config/router.yaml ---------------------------+
                         |  models, per-task routing, pattern settings, breaker + cache settings    |
                         +-----------------------------------+--------------------------------------+
                                                             |
 review --> Orchestrator (Router | Chain | Fan-Out | Cascade) --> ModelRouter (which model?)
                |
                v
          ModelGateway  (middleware, applied to every model call)
   +------------------------------------------------------------------------+
   | input contract -> Cache (Redis) --hit--> result (cost 0)               |
   |        | miss                                                          |
   |        v                                                               |
   | CircuitBreaker --open--> fallback result (fallback=true) + retry queue |
   |        | closed / probing                                              |
   |        v                                                               |
   | Anthropic call (per-task timeout + retries) -> output contract check   |
   |        |                       \--invalid--> DLQ + fallback result     |
   |        v                                                               |
   | CostTracker (JSON log line) -> cache write (30d TTL)                   |
   +------------------------------------------------------------------------+
                |
                v
   Dagster assets:  review_text -> sentiment -> aspect (negative only)
                    -> toxicity (unsafe flagged only) -> final_review_enrichment
                |
                v
   ReviewResultsIOManager --> idempotent MERGE store
        review_results | review_results_fallback | needs_human_review (toxicity conf < 0.7)
```

### The four patterns (`src/patterns.py`)

| Pattern | Flow | Article latency p50 / p95 / p99 |
|---------|------|---------------------------------|
| Router | simple (<100 words, no keywords) -> Haiku for all tasks; complex (>300 words or keyword) -> Sonnet | 250ms / 900ms / 1.8s |
| Chain | Haiku sentiment -> if negative: Sonnet aspects -> if unsafe language: Opus toxicity | 800ms / 2.5s / 4.2s |
| Fan-Out | Haiku, Sonnet, Opus in parallel, merged (latency = max) | 1.5s / 2.0s / 3.0s |
| Cascade | Haiku -> (confidence < 0.7) Sonnet -> fallback + human review queue | 220ms / 950ms / 2.5s |

The article's latency table is stored in `src/latency.py` as `ARTICLE_PROFILES` for comparison
against whatever `LatencyRecorder` measures. This repo does not reproduce those numbers (see below).

## Tech Stack

| Component | Tool |
|-----------|------|
| AI Models | Claude Haiku (`claude-haiku-4-5-20251001`), Sonnet (`claude-sonnet-5-5`), Opus (`claude-opus-5-5`) |
| Orchestration | Dagster (assets, I/O manager, retry policies, schedule) |
| Cache | Redis (ElastiCache / Memorystore) via `redis-py`, in-memory fake for tests |
| Config | YAML + dataclasses + python-dotenv |
| Contracts | Pydantic v2 |
| Transformation | dbt (two example models, not executed) |
| Language | Python 3.10+ |

Haiku's model id and pricing match Article 1. Override any id with `HAIKU_MODEL`, `SONNET_MODEL`, `OPUS_MODEL`.

## Project Structure

```
article-04-multi-model-orchestration/
├── config/
│   └── router.yaml                # models, task routing, patterns, breaker, cache
├── src/
│   ├── config.py                  # env + YAML config loading and validation
│   ├── schemas.py                 # data contracts, fallback payloads, TaskResult
│   ├── router.py                  # ModelRouter, complexity score, aspect retriever
│   ├── gateway.py                 # middleware: cache, breaker, retries, contracts, cost
│   ├── patterns.py                # Router / Chain / Fan-Out / Cascade
│   ├── cache.py                   # CacheBackend protocol, RedisCache, InMemoryCache, ReviewCache
│   ├── circuit_breaker.py         # per-model breaker + registry + fallback-rate alert
│   ├── cost_tracker.py            # per-call cost logging and aggregation
│   ├── results_store.py           # composite key, MERGE SQL, table routing
│   ├── latency.py                 # p50/p95/p99 recorder
│   ├── lineage.py                 # cross-model lineage rows
│   ├── queues.py                  # retry / human review / DLQ stand-ins
│   ├── prompts.py                 # task prompts
│   └── factory.py                 # convenience wiring
├── dags/
│   └── review_enrichment_assets.py  # Dagster assets, I/O manager, daily 2am schedule
├── dbt_models/marts/
│   ├── fct_model_cost_summary.sql
│   └── fct_review_lineage.sql
├── tests/                         # pytest, fully offline
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env     # only needed for live runs

# Run the whole test suite (no keys, no network, no Redis)
python3 -m pytest tests -q
```

Local run against the real API (requires `ANTHROPIC_API_KEY`; falls back to an in-memory cache if Redis is unreachable):

```python
from src.factory import build_orchestrator

orch = build_orchestrator()
out = orch.run_chain({"review_id": "rev_1", "text": "Battery is fine but the app is buggy."})
print(out.total_cost, out.latency_ms, orch.recorder.report_all())
print(orch.gateway.cost.summary())
```

## Tests

`tests/` covers: config parsing/validation, router golden datasets, all four patterns,
cache (TTL, hit/miss, fail-open, Redis adapter), circuit breaker state machine, gateway
middleware (retries, timeouts, rate limits, contract violations, DLQ, retry queue),
cost aggregation, idempotent MERGE behaviour, percentile maths, and Dagster
materialization (conditional execution, retry policies, I/O manager routing).

## What is mocked / not tested against live services

Everything in the test suite runs offline. Honestly:

- **Anthropic**: replaced by `FakeAnthropic` in `tests/conftest.py`. Prompts, real model output quality, real token counts, real timeouts and real rate-limit behaviour are untested. Token usage in tests is a fixed 120 in / 15 out.
- **Redis**: tests use `InMemoryCache`; `RedisCache` is tested only against a `MagicMock` client (`get` / `setex` calls). Never run against a real server, so connection pooling, eviction and serialization at scale are unverified.
- **boto3 / AWS (API Gateway, Lambda, SQS, ECS, CloudWatch)**: not used by the code. The cloud architecture in the article is not implemented. Queues are in-memory stand-ins; the cost log is emitted through Python `logging`, not CloudWatch.
- **Snowflake / BigQuery**: not used. `InMemoryResultStore` mimics MERGE semantics; `MERGE_SQL` and the dbt models are the article's SQL and were never executed against a warehouse.
- **Embeddings / vector store**: `KeywordAspectRetriever` is a lexical stand-in; the 0.8 similarity threshold is meaningful only for this stand-in, not for real embeddings.
- **Dagster**: materialized in-process with `dagster.materialize`. Not run in a deployed instance; the daily schedule is defined but never triggered; per-call timeouts live in the gateway, not as Dagster op timeouts; `pool` concurrency limits are declared but not enforced by an instance configuration (in-process concurrency is bounded by `run_concurrently`).
- **Performance numbers**: the article's p50/p95/p99 profiles, cache hit rates (10-15%), and cost figures are *not* reproduced or benchmarked here. Tests assert pattern behaviour (call counts, concurrency, ordering), not production latency.
- **Concurrency**: thread-safety is exercised by the fan-out and bounded-parallelism tests only, not by load tests.
