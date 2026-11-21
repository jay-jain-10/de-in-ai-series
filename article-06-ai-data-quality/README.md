# Article 6: The Semantic Data Quality Layer Your Warehouse Is Missing

**Two-layer validation (syntactic rules + semantic AI sampling) with a human feedback loop, for healthcare claims**

Part 6 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

All data in this folder is **synthetic**. No real patient data is used or required.

---

## Overview

Syntactic validators pass a claim with diagnosis `J01.90` (acute sinusitis) and procedure `27447` (total knee replacement): both codes are valid, nothing is null. This project adds the layer that notices.

- **Layer 1 (syntactic):** a rule engine driven by a versioned YAML/JSON rule file, applied to 100% of rows. Failures go to quarantine.
- **Layer 2 (semantic):** Claude Sonnet with few-shot domain examples, applied to a risk-based sample of Layer 1 survivors (100% high / 20% medium / 3% low). Output: `{is_consistent, anomaly_type, confidence, reasoning}`. Flags above the domain confidence threshold go to quarantine.
- **Quarantine table:** an audit log keyed on `(record_id, check_id)`, so retries never double-quarantine.
- **Human review queue + feedback loop:** `confirm_error` / `accept_as_is` / `fix_upstream` decisions drive rule, prompt and sampling recommendations.
- **Calibration protocol:** monthly 200-sample review, two reviewers, Cohen's kappa >= 0.7, precision >= 80%.
- **Quality as a dimension:** `dim_quality` (SQL DDL + dbt model) joins back to fact tables.

## Architecture

```
                    rules/healthcare_claims_v1.yaml (versioned, PR-reviewed)
                                   |
 S3 raw claims --> LAYER 1: RuleEngine (100% of rows) ----FAIL----> Quarantine
                                   | PASS                          (record_id, check_id,
                                   v                                layer, anomaly_type,
                    RiskBasedSampler (deterministic hash)           confidence, decision)
                      high 100% | medium 20% | low 3%                  ^      |
                                   |                                   |      v
                                   v                                   |  Human review queue
                    LAYER 2: Claude Sonnet + few-shot ---ANOMALY-------+  confirm_error /
                    {is_consistent, anomaly_type, confidence}             accept_as_is /
                                   | CONSISTENT                           fix_upstream
                                   v                                          |
                           Production records                                 v
                                                              Feedback loop: relax rule, add
 Quarantine --MERGE--> Snowflake QUALITY.quarantine_record    examples, raise sampling;
        --dbt--> stg_quarantine_records --> dim_quality       monthly calibration (200 samples,
                                        --> fct_source_quality kappa >= 0.7, precision >= 80%)

 Orchestration: Airflow  ingest > layer1 > sample > semantic_validate > persist > dbt > SLA/metrics
```

## Tech Stack

| Component | Tool |
|-----------|------|
| Semantic AI model | Claude Sonnet (claude-sonnet-5-5) |
| Orchestration | Apache Airflow |
| Landing zone / sample cache | AWS S3 (boto3) |
| Quarantine metadata | PostgreSQL DDL provided; SQLite used for local runs and tests |
| Warehouse | Snowflake |
| Transformation | dbt |
| Language | Python 3.10+ |

## Project Structure

```
article-06-ai-data-quality/
├── src/
│   ├── rule_engine.py         # Layer 1: YAML/JSON-driven syntactic rules
│   ├── risk_sampler.py        # Risk score, tiers, deterministic sampling, cost math
│   ├── semantic_validator.py  # Layer 2: Claude Sonnet + few-shot, retries, schema
│   ├── quarantine.py          # Idempotent quarantine/audit store
│   ├── review_queue.py        # Human review queue + feedback report + SLA
│   ├── calibration.py         # 200-sample draw, Cohen's kappa, precision/recall
│   ├── pipeline.py            # run_layer1 / run_layer2 orchestration
│   ├── quality_loader.py      # Snowflake MERGE loader
│   ├── s3_io.py               # S3 read/write helpers
│   ├── data_generator.py      # Synthetic claims with injected errors
│   └── config.py              # Thresholds, rates, model ids, env config
├── rules/
│   └── healthcare_claims_v1.yaml   # Versioned Layer 1 rules + Layer 2 examples
├── sql/
│   ├── quarantine_record.sql  # Postgres DDL (quarantine audit log)
│   └── dim_quality.sql        # Snowflake DDL (landing table + quality dimension)
├── dags/
│   └── healthcare_quality_dag.py   # Daily DAG + monthly calibration DAG
├── dbt_models/
│   ├── staging/ (stg_quarantine_records.sql, source_risk_score.sql, schema.yml)
│   └── marts/   (dim_quality.sql, fct_source_quality.sql)
├── tests/
├── .env.example
├── requirements.txt
└── README.md
```

## Running the tests

```bash
pip install -r requirements.txt     # or: pip install anthropic boto3 snowflake-connector-python pydantic python-dotenv pyyaml pytest
python3 -m pytest -q
```

Tests run fully offline. Anthropic, boto3, Snowflake and Airflow are mocked; no keys are needed.

## Key numbers (all encoded in `src/config.py` and tested)

| Item | Value |
|------|-------|
| Sampling rates | high 100% / medium 20% / low 3% (score >80 / 50-80 / <50) |
| Alert thresholds | healthcare 0.60, financial 0.70, marketing 0.80 |
| Calibration | 200 samples/month, kappa >= 0.7, precision >= 80% |
| Review sampling | 10% of quarantined L1 records (L2 flags always queued) |
| SLAs | L1 10 min, L2 2 h, human review 48 h |
| Cost math | 1M/4M/5M records -> $3,000 + $2,400 + $450 = $5,850 vs $30,000 unsampled |

## What is mocked / not tested against live services

Being direct about the limits of this test suite:

- **Claude is never called.** Tests use a mock client. The end-to-end tests use a deterministic "oracle" that encodes the same rules as the few-shot examples. They prove the plumbing (prompt assembly, parsing, thresholds, routing, idempotency), **not** that Sonnet will achieve any particular precision or recall on real claims. The article's 85-95% figures are not reproduced here.
- **Snowflake is mocked.** The MERGE SQL and the dbt models are never executed against Snowflake. They are only checked structurally (string/column checks). `dim_quality.sql` and the dbt models use Snowflake functions (`MIN_BY`, `COUNT_IF`, `IFF`) and have not been run through `dbt compile` or `dbt test`.
- **S3 (boto3) is mocked.** Pagination and error paths against real S3 are untested.
- **Airflow is stubbed.** `airflow` is not required to run the tests. The DAG file is loaded with fake `DAG`/operator classes to verify task wiring, timeouts, schedule, and that task callables are retry-safe. It has not been parsed by a real Airflow scheduler.
- **Quarantine store is SQLite**, not PostgreSQL. The `ON CONFLICT DO NOTHING` semantics are the same, but `sql/quarantine_record.sql` (JSONB, UUID, partial index) is not executed anywhere.
- **Not covered:** concurrency (multiple writers), Layer 1 performance at 10M rows (the 2-3 minute figure is untested; the engine is pure Python and does not push down to SQL/Great Expectations), real cost/latency, Haiku/Sonnet model tiering, AWS/GCP infrastructure, QuickSight dashboards.
- The data are synthetic, with a small hand-built diagnosis/procedure vocabulary. They are not clinically validated.
