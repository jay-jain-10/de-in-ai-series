# Article 7: FinOps for AI Pipelines

**Cost engineering: caching, model tiering, prompt optimization, batching, and cost as first-class data**

Part 7 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

Companion code for the article's four-lever cost framework. It provides a cost-tracking wrapper around Claude calls (every AI row carries `cost_tokens` / `cost_usd`; every call writes a `cost_events` row), one module per lever, a waterfall calculator that reproduces the article's `$150K -> $90K -> $42K -> $31.5K -> $15.75K` sequence, cost SLAs ($0.005/record, $200/day, 2x variance), a backfill cost estimator, a CloudWatch-style metric emitter, SQL DDL and dbt models for aggregation, and a Lambda wrapper handler.

## Architecture

```
 record
   |
   v
 [Lambda wrapper] --> SLA check (daily budget? throttle/pause -> 429)
   |
   v
 [Lever 1: cache]  exact SHA256 (30d TTL) / semantic cos > 0.95 --hit--> cost_usd = 0
   | miss
   v
 [Lever 2: tiering] score 0-10: <3 Haiku, <7 Sonnet, else Opus
   |
   v
 [Lever 3: prompt optimization] terse system prompt, JSON out, 2 few-shot
   |
   v
 Claude (Anthropic API) --invalid output--> retry on Sonnet (is_retry=true)
   |
   v
 CostEvent (cost_tokens, cost_usd, attempt, cache_hit, ...)
   |--> Firehose --> S3 + Snowflake RAW.cost_events --> dbt (fct_daily_cost,
   |                                                    fct_cost_anomalies, fct_cost_by_model)
   |--> MetricEmitter --> CloudWatch (daily_cost, cache_hit_rate, ... + alarms)

 Offline path (Lever 4): BatchAccumulator -> Anthropic Message Batches API (50% off)
 Planning: waterfall.py (savings) and backfill_estimator.py (approval gate)
```

## Tech Stack

| Component | Tool |
|-----------|------|
| AI Models | Claude Haiku (claude-haiku-4-5-20251001), Sonnet, Opus (ids configurable in `.env`) |
| Event stream | AWS Lambda, Kinesis Data Firehose |
| Metrics | CloudWatch custom metrics and alarms |
| Cache | S3 (Intelligent-Tiering) or in-memory |
| Warehouse | Snowflake |
| Transformation | dbt |
| Language | Python 3.10+ |

## Project Structure

```
article-07-cost-engineering/
├── src/
│   ├── config.py               # Anthropic/AWS/Snowflake config, SLA and lever settings
│   ├── pricing.py              # Price table ($15/$3/$0.80 input) and cost math
│   ├── cost_events.py          # CostEvent, annotate_row, Firehose/Snowflake/in-memory sinks
│   ├── cost_tracker.py         # CostTrackingClient: the cost wrapper around Claude
│   ├── cost_sla.py             # $0.005/record, $200/day, 2x variance; SLAEnforcer
│   ├── levers/
│   │   ├── caching.py          # Lever 1: content hash + semantic dedup, S3/in-memory
│   │   ├── model_tiering.py    # Lever 2: complexity score, router, Haiku->Sonnet fallback
│   │   ├── prompt_optimization.py  # Lever 3
│   │   └── batching.py         # Lever 4: accumulator, Batches API submit/collect
│   ├── pipeline.py             # Cache -> tier -> optimize -> call -> fallback
│   ├── waterfall.py            # Waterfall + worked-example calculator and reconciliation
│   ├── backfill_estimator.py   # Backfill estimate + approval gate
│   ├── metrics.py              # CloudWatch-style emitter and alarm definitions
│   ├── snowflake_loader.py     # Creates cost_events from sql/cost_events_ddl.sql
│   └── lambda_handler.py       # Lambda cost wrapper handler
├── sql/
│   └── cost_events_ddl.sql
├── dbt_models/
│   ├── staging/{stg_cost_events.sql, schema.yml}
│   └── marts/{fct_daily_cost.sql, fct_cost_anomalies.sql, fct_cost_by_model.sql}
├── tests/
├── pytest.ini
├── .env.example
├── requirements.txt
└── README.md
```

## Running Tests

```bash
pip install -r requirements.txt
python3 -m pytest
```

All tests run offline with no API keys.

## Article arithmetic: known discrepancies

The code implements the article's constants faithfully (e.g. tiering factor 0.47) and `waterfall.reconcile_waterfall()` / `reconcile_ticket_example()` list where the article's own math does not reconcile. Highlights: the tiering formula evaluates to 0.56, not 0.47 (unrounded, the waterfall would end at $18.9K / 87.4%); the ticket example's "$97 / $111.40 / $399.20 saved" figures double count or mix total and API cost (recomputed: $68 / $82.40 / $99.20); "100K tickets = $633.60" doubles fixed infrastructure (it would be $333.60).

## What is mocked / not tested against live services

- **Anthropic API**: all calls use a scripted fake client. Token counts and prices in tests are synthetic. The Message Batches call shapes (`batches.create`, `retrieve`, `results`) were checked against the Anthropic docs but never run against the live API. **Pricing caveat:** the default price table in `src/pricing.py` and the Opus tier in `src/waterfall.py` follow the article ($15/$75 Opus, $0.80/$4 Haiku, $3/$15 Sonnet). Current list prices differ (e.g. Haiku 4.5 is $1/$5, Opus 4.5+ is $5/$25), so update the table from the pricing page before trusting absolute dollar figures.
- **Output prices** are not in the article; 5x input is an assumption (Haiku $4, Sonnet $15, Opus $75). Verify against current Anthropic pricing before relying on dollar figures.
- **boto3** (Firehose, CloudWatch, S3): MagicMock clients. No real streams, alarms, metrics or buckets were created; alarm definitions are not validated by CloudWatch.
- **Snowflake**: connection is a MagicMock. DDL and dbt SQL are never executed or compiled (no `dbt parse/run`), so Snowflake-specific syntax errors are possible.
- **Embeddings**: semantic dedup uses injected toy vectors, not a real embedding model.
- **Complexity scoring heuristics** are illustrative; the article prescribes the idea (length, keywords, entities, reasoning) not the weights, and nothing here is validated for accuracy against Haiku/Sonnet A/B tests.
- **Lambda**: handler is called directly; no packaging, cold-start, or timeout behaviour tested. The daily-spend enforcer is per-process memory, so it does not enforce a global budget across concurrent Lambda containers (use the `cost_events` table for that).
- The AWS ~$300/month and GCP ~$130/month infrastructure costs are not modeled.
