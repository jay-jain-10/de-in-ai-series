# Article 8 (Capstone): From Pipeline to Platform

**Building an AI Data Platform Your Whole Org Can Use**

Part 8 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

A compact reference implementation of the platform described in the capstone article: one **AI Gateway** that every team calls instead of calling Claude directly, plus the shared services around it (ingestion, document processor, two-tier validation, quarantine and human review, lineage, observability) and **self-service pipeline onboarding from YAML**. The sample onboarding is the insurance-claims pipeline (500-1000 claims/day, five ingestion channels); a second YAML (compliance contracts) shows the platform generalizing.

What is implemented:

- **AI Gateway (FastAPI):** `/v1/extract`, `/classify`, `/summarize`, `/validate`, `/score`; API-key auth with per-team daily quotas (claims: 5000 calls/day, HTTP 429 with a clear error); model routing (extract and validate to Sonnet, classify and summarize to Haiku, score to Haiku or Sonnet by complexity); two-level cache (L1 Redis 1-day TTL, L2 S3 30-day TTL, keyed by request-content hash namespaced per team); cost attribution by `caller_metadata` (team, pipeline, task_type) with cost events streamed to Kafka; retries with backoff; structured-output enforcement (retry with explicit instruction, then fall back to Haiku); circuit breaker (opens above 10% error rate; one probe at a time when half-open). Only rate-limit / connection / 5xx errors are retried and counted by the breaker; 4xx errors return 500 without tripping it for other teams.
- **Ingestion:** contract check (`source_type`, `source_id`, `timestamp` required), content-hash dedup, publish to `documents.incoming`.
- **Document processor:** pluggable extractors (text, email, PDF native/scanned, image OCR), partial-failure handling (`extraction_confidence = 0.6`), overlap chunking with every chunk under 4000 tokens.
- **Validation (two tiers):** Layer 1 deterministic rules plus domain rules registered by business teams; Layer 2 sampled semantic validation through the gateway.
- **Quarantine + human review:** review queue, confirm/correct/upstream decisions, release hook back into the warehouse, accuracy tracking (alert below 95%), 48h overdue detection.
- **Observability:** metrics registry, cost and quality dashboards, SLA monitor for the four published SLAs.
- **Lineage:** every step logs timestamp, processing_version, model, tokens, cost, confidence; persisted to the warehouse.
- **Data contracts:** Pydantic models at every boundary (`src/contracts.py`), versioned.
- **Backfill:** find documents by `processing_version`, resubmit raw text, MERGE results.
- **Governance bits:** prompt registry with versions, model catalog of approved models.

## Architecture

```
 5 sources                                        AI GATEWAY (FastAPI, /v1/*)
 email  portal fax api docusign                  +-------------------------------------+
    \     |    |   |    /                         | auth (X-API-Key) -> team quota      |
     v    v    v   v   v                          | cache  L1 Redis(1d) -> L2 S3(30d)   |
  IngestionService (contract check, content-hash  | breaker (>10% errors) -> defaults   |
  dedup) --> Kafka: documents.incoming            | router: extract/validate=Sonnet     |
                |                                 |         classify/summarize=Haiku    |
                v                                 | retries -> JSON enforce -> Haiku    |
        DocumentProcessor                         | cost events -> Kafka cost topic     |
   (pdf/image/email extractors, chunk <4000 tok)  +----------------^--------------------+
                |                                                  |
                v                                                  |
        PipelineRunner  (one per YAML-registered pipeline) --------+
   extract -> Layer1 (rules) -> classify risk -> score -> Layer2 (sampled, semantic)
                |                         |                      |
          pass  |                  fail   v                fail  v
                v                  Quarantine + Human Review (confirm / correct / upstream)
   Warehouse MERGE (raw_documents, extracted_claims,         |
   validation_results, fraud_scores, cost_events, lineage)   | release hook (re-validate, load)
                |  <-----------------------------------------+
                v
   route handler (claims -> mainframe topic, compliance -> workflow engine)

 Cross-cutting: Lineage recorder | MetricsRegistry | Dashboards | SLAMonitor | PipelineRegistry (YAML onboarding)
```

## Tech Stack

| Component | Tool |
|-----------|------|
| AI Models | Claude Haiku (`claude-haiku-4-5-20251001`), Claude Sonnet (`claude-sonnet-5-5`, override with `EXTRACTION_MODEL`) |
| Gateway | FastAPI |
| Contracts | Pydantic v2 |
| Pipeline definitions | YAML (PyYAML) |
| Streaming | Kafka (`confluent-kafka` adapter) |
| Cache | Redis (L1), S3 via boto3 (L2) |
| Warehouse | Snowflake (MERGE loader, same pattern as Article 1) |
| Tests | pytest, FastAPI TestClient |
| Language | Python 3.10+ |

## Project Structure

```
article-08-capstone/
├── src/
│   ├── config.py               # Config dataclasses (Article 1 conventions, env-driven)
│   ├── contracts.py            # Pydantic data contracts at every boundary
│   ├── messaging.py            # Publisher protocol, in-memory + Kafka adapter
│   ├── ingestion.py            # Contract check, dedup, publish to documents.incoming
│   ├── document_processor.py   # Pluggable extractors + overlap chunking
│   ├── validation.py           # Layer 1 rules, rule registry, Layer 2 sampled semantic
│   ├── quarantine.py           # Quarantine store, review queue, hooks, accuracy metrics
│   ├── lineage.py              # End-to-end lineage recorder
│   ├── observability.py        # Metrics, dashboards, SLA monitor
│   ├── warehouse.py            # In-memory + Snowflake MERGE writers, DDL
│   ├── registry.py             # YAML pipeline definitions + onboarding checks
│   ├── pipeline.py             # PipelineRunner, backfill, Platform wiring
│   ├── domains/
│   │   ├── claims.py           # Claims rules, ClaimRecord contract, mainframe router
│   │   └── compliance.py       # Workflow-engine router (second team)
│   └── gateway/
│       ├── app.py              # FastAPI app + build_service() wiring
│       ├── service.py          # Gateway core (cache, breaker, retries, JSON enforcement)
│       ├── cache.py            # InMemory / Redis / S3 backends, TieredCache
│       ├── circuit_breaker.py
│       ├── quota.py            # API-key auth, team accounts, daily quotas
│       ├── cost.py             # Cost attribution + Kafka cost events + budget alerts
│       ├── router.py           # Model catalog + routing policy
│       ├── prompts.py          # Versioned prompt registry
│       ├── llm.py              # LLMClient protocol + Anthropic adapter
│       └── client.py           # SDK for business teams (gateway.extract(...))
├── pipelines/
│   ├── claims-processing.yaml  # Sample self-service onboarding (insurance claims)
│   └── compliance-contracts.yaml
├── config/teams.yaml           # Teams, quotas, priorities, budgets (keys from env)
├── tests/                      # 152 offline tests
├── pytest.ini
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

```bash
pip install -r requirements.txt     # tests only need fastapi, httpx, pydantic, pyyaml, pytest, anthropic, python-dotenv
cp .env.example .env                # add real keys only for live deployment
```

Wire it up (the same pieces the tests use):

```python
from src.config import PlatformConfig
from src.gateway.app import build_service, create_app
from src.gateway.llm import AnthropicLLMClient
from src.gateway.quota import load_teams
from src.pipeline import Platform

cfg = PlatformConfig()
service, auth = build_service(cfg, AnthropicLLMClient(cfg.anthropic.api_key), load_teams(cfg.gateway.teams_file))
app = create_app(service, auth)                      # uvicorn module:app
platform = Platform(cfg, auth)                       # pass real Publisher / Warehouse here
platform.onboard(open("pipelines/claims-processing.yaml").read(), gateway_client)
```

## How to Run Tests

```bash
cd article-08-capstone
python3 -m pytest
```

Tests are fully offline: no API keys, no network.

## What Is Mocked / Not Tested Against Live Services

Be clear-eyed: this code has never talked to a real Claude, Kafka, Redis, S3 or Snowflake.

| Dependency | In tests | Not verified |
|------------|----------|--------------|
| Anthropic API | `FakeLLM` returns scripted JSON; `AnthropicLLMClient` tested with a `MagicMock` SDK client | Real response shapes, real rate-limit behaviour, real token counts, prompt quality and extraction accuracy (the 98% / 92% figures in the article are not measured here) |
| Kafka | `InMemoryPublisher`; `KafkaPublisher` tested against a `MagicMock` producer | Delivery semantics, partitioning, consumer side (nothing here consumes `documents.incoming`; `run()` is called directly) |
| Redis | In-memory cache; `RedisCache` tested with `MagicMock` | Connection handling, eviction, latency (<5ms claim) |
| S3 | `FakeS3` object store double | IAM, lifecycle rules, eventual consistency, real `ClientError` shapes |
| Snowflake | `InMemoryWarehouse`; `SnowflakeWarehouse` tested for generated MERGE SQL and bound params with a `MagicMock` connection | The SQL has not been executed on Snowflake; DDL in `warehouse.py` is untested; no VARIANT/PARSE_JSON handling (JSON stored as strings) |
| PDF / OCR | Extractor backends injected as stub callables | pdfplumber, Textract, Tesseract integrations are not included |
| Orchestration | None | No Dagster/Airflow DAG; `PipelineRunner.run()` chains the stages in-process |
| Postgres | None | The article's gateway activity log in PostgreSQL is replaced by in-memory cost events plus the warehouse tables |
| Load / latency | Thread-safety of quota, breaker and metrics; two concurrent requests in flight through the app | No throughput or SLA testing. Quota, breaker and cache state are per-process (thread-safe within one process, NOT shared across gateway replicas; production needs Redis-backed counters) |

## Deviations from the Article

- **Pricing:** configurable per-1M-token prices using Article 1's Haiku rates ($1/$5) and Sonnet at $3/$15, not the article's $0.003/$0.006 per 1K Bedrock figures.
- **Request prioritization/queueing:** quota exhaustion fails fast with 429 (plus `Retry-After`). Teams carry a `priority` field in `teams.yaml` but no priority queue is implemented.
- **Chunking threshold:** the article chunks above 5000 tokens but requires chunks under 4000 tokens; this code chunks anything that would not fit under 4000.
- **Layer 2** runs inline for sampled records rather than on a separate async worker (sampling is deterministic by document id; high-risk records are always sampled).
- **Circuit breaker** "recovery" uses standard closed/open/half-open semantics (the article's wording is inverted).
- **Cache scope:** the article keys the cache by request content alone. Here the key also includes the authenticated team, so teams do not share cache entries. A shared key would let one team learn, from `cached=true` or latency, that another team already submitted a given document. The cost is fewer cache hits across teams.
- **Degraded gateway is fail-closed in pipelines:** if risk classification or Layer 2 falls back to defaults, the record is quarantined rather than loaded on a made-up risk class. A degraded fraud score is stored as unscored (`null`), not as the 0.5 default.
- Gateway also exposes `/summarize`; `/score` is kept from the article.
- Sonnet model id (`claude-sonnet-5-5`) is an assumption; override via `EXTRACTION_MODEL`.
