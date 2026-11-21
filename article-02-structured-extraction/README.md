# Article 2: The Unstructured Data Problem at Enterprise Scale

**Extracting Structure from Chaos**

Part 2 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

A six-stage pipeline that turns vendor-contract PDFs (digital, scanned, or hybrid) into validated, versioned rows in Snowflake: payment terms, termination conditions, liability cap, and (since extraction_version 2.0) governing law. Every extracted value carries a confidence score, a page reference and the exact source span, and is routed by a trust pyramid (production / flagged / quarantine / dead-letter queue).

## Architecture

```
S3 (PDFs) -> SNS -> Step Functions
                      |
   Stage 1  Format detection (Lambda, PyPDF2 per-page: digital / scanned / hybrid)
                      |
   Stage 2  Text extraction  digital -> PyPDF2
                             scanned -> Textract (tables + confidence) -> Tesseract fallback
                      |
   Stage 3  Chunking (Claude Haiku finds section boundaries; sentence-split fallback)
                      |
   Stage 4  Extraction (Claude Sonnet + Pydantic ClauseExtraction; re-prompt on bad schema)
            Lambda for small docs, ECS Fargate for > 30 pages
                      |
   Stage 5  Validation (format, 15-180 day range, span-in-source hallucination check)
                      |
        +-------------+----------------+-----------------------+
        | >= 0.95     | 0.75 - 0.95    | 0.5 - 0.75 / failed   | 0 / hard failure
        v             v                v validation            v
   production    production +     quarantine table +       Stage 6  DLQ
   table         review badge     human review queue       s3://bucket/quarantine/
        \_____________/            (24h SLA)               <id>/error.json + PDF
               |
        Snowflake MERGE on (contract_id, extraction_version)  ->  dbt
```

## Tech Stack

| Component | Tool |
|-----------|------|
| Segmentation model | Claude Haiku (`claude-haiku-4-5-20251001`) |
| Extraction model | Claude Sonnet (`claude-sonnet-5-5`) |
| Text extraction | PyPDF2, AWS Textract, Tesseract (pytesseract + pypdfium2) |
| Schema contract | Pydantic v2 |
| Validation | Expectation-style pure-Python checks (see notes) |
| Orchestration | AWS Step Functions (+ Lambda, ECS Fargate) |
| Dead-letter queue | S3 `quarantine/` prefix |
| Warehouse | Snowflake (MERGE) |
| Transformation | dbt |
| Language | Python 3.10+ |

## Project Structure

```
article-02-structured-extraction/
├── src/
│   ├── config.py              # Models, thresholds, versions
│   ├── models.py              # Pydantic contracts (ClauseExtraction, ContractExtraction...)
│   ├── format_detector.py     # Stage 1
│   ├── text_extractor.py      # Stage 2 (Textract / Tesseract fallback)
│   ├── chunker.py             # Stage 3 (Haiku segmentation)
│   ├── extractor.py           # Stage 4 (Sonnet + schema enforcement)
│   ├── validator.py           # Stage 5 (checks + confidence routing)
│   ├── dlq.py                 # Stage 6 (quarantine + daily summary)
│   ├── snowflake_loader.py    # Idempotent MERGE, quarantine + review tables
│   ├── schema_evolution.py    # Column registry, ALTER TABLE ADD COLUMN
│   ├── pipeline.py            # End-to-end orchestration for one contract / batch
│   ├── lambda_handlers.py     # Handlers used by the state machine
│   ├── sla.py                 # The three SLAs
│   ├── cost_model.py          # Article's cost breakdown
│   └── llm_utils.py           # Claude call + JSON parsing helpers
├── step_functions/
│   └── state_machine.json     # ASL definition
├── sql/
│   ├── extracted_contracts_merge.sql
│   ├── schema_evolution.sql
│   └── backfill_candidates.sql
├── dbt_models/
│   ├── staging/{stg_extracted_contracts.sql, schema.yml}
│   └── marts/fct_contract_comparison.sql
├── tests/                     # 112 offline pytest tests
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in credentials for live runs
```

```python
import boto3, anthropic
from src.config import PipelineConfig
from src.dlq import DeadLetterQueue
from src.pipeline import ContractPipeline
from src.snowflake_loader import SnowflakeLoader
from src.text_extractor import TextExtractor, TextractOCR

cfg = PipelineConfig()
s3 = boto3.client("s3")
pipeline = ContractPipeline(
    anthropic.Anthropic(api_key=cfg.anthropic.api_key),
    TextExtractor(cfg, textract=TextractOCR(boto3.client("textract"))),
    DeadLetterQueue(s3, cfg),
    SnowflakeLoader(cfg.snowflake),
    cfg,
)
pdf = s3.get_object(Bucket=cfg.s3.bucket, Key="contracts/acme.pdf")["Body"].read()
print(pipeline.process_contract("vendor-acme-2024-001", pdf, "contracts/acme.pdf"))
```

## Running Tests

```bash
pip install -r requirements.txt
python3 -m pytest -v
```

Tests run fully offline; no API keys, AWS credentials or Snowflake account needed.

## What Is Mocked / Not Tested Against Live Services

Everything external is faked. Passing tests show the logic is internally consistent, not that it works in production.

- **Anthropic API**: replaced by a `FakeAnthropic` client with scripted replies. Prompt quality, real model accuracy, hallucination rates and the model ids themselves are untested. Segmentation and extraction prompts have never been tuned on real contracts.
- **AWS Textract**: boto3 client is a `MagicMock` returning hand-built `Blocks`. Real response shapes (pagination, `PARTIAL_SUCCESS`, rotated pages, table cell relationships) are only approximated; table data is counted, not parsed into rows.
- **Tesseract**: the engine is injected as a mock function; the real `tesseract_ocr` (pypdfium2 render + pytesseract) is never executed in tests.
- **S3 / DLQ**: `MagicMock` S3 client; no real buckets, event notifications, SNS or IAM.
- **Snowflake**: a fake connection records SQL. The MERGE, `CREATE TABLE`, `ALTER TABLE` and `PARSE_JSON` staging inserts have **not** been executed against Snowflake, so dialect errors are possible. Idempotency is verified only at the SQL-generation / statement-sequence level. The loader runs all DDL (which auto-commits in Snowflake) before an explicit `BEGIN`, and deletes a re-routed `(contract_id, extraction_version)` from the other table so a route change on re-run leaves no stale row.
- **Step Functions / Lambda / Fargate**: `state_machine.json` is checked structurally (references resolve, all states reachable, catches present) but never deployed or executed. The Lambda handlers for text extraction, chunk+extract, validate and load named in the ASL are not implemented as separate handlers (see below); Tesseract-in-Lambda packaging is not addressed.
- **dbt**: models are illustrative and not compiled or run.
- **PDFs**: test PDFs are tiny synthetic files; real-world layouts (multi-column, handwritten, mixed rotation) are not covered.

## Deviations From the Article

- **Great Expectations** is not used; its checks (format, range, hallucination, median confidence) are plain functions in `validator.py`.
- **Trust pyramid gap**: the article leaves (0, 0.5) undefined and mentions both a 0.5 and 0.7 review boundary. Here: >= 0.95 production, 0.75-0.95 flagged, (0, 0.75) quarantine, 0 / no clauses -> DLQ; `review_required` is set when confidence < 0.7, any validation flag, or quarantine. Any failed format/range/hallucination check forces quarantine regardless of confidence; a soft flag (missing or out-of-range page reference) downgrades `production` to `production_flagged` so the row is never served unbadged while awaiting review.
- **Rotation detection** is not implemented explicitly (Textract handles it natively).
- **Tesseract labor cost**: the article's "25,000 minutes / ~$21,000" does not follow from its inputs (15,000 minutes = $12,500, which matches its other sections); the cost model uses $12,500.
- The state machine references Lambda functions (extract text, Tesseract, chunk/extract, validate, load) that are represented in this repo by the library code and `ContractPipeline`, not individual handlers.
