# AI-Powered Invoice Processing on AWS

Code for the 3-part Medium series **"AI + Data Engineering on AWS"**.

**The problem:** a finance team manually keys ~4,000 vendor invoices a month. Error rate ~8%, roughly $345K/year in labor plus leakage from duplicate payments and late fees. Off-the-shelf OCR topped out around 72% accuracy.

**The solution:** PDF lands in S3 -> EventBridge -> Step Functions -> Textract (read) -> Claude on Bedrock (classify + extract) -> validate -> DynamoDB, with SNS alerts and a human review queue for anything doubtful.

| Part | Article | Code in this folder |
|------|---------|---------------------|
| 1 | We Were Drowning in Invoices (problem, system design, cost) | architecture only, see `articles/` |
| 2 | Show Me the Code | `schemas.py`, `textract_extractor.py`, `llm_extractor.py`, `validator.py`, `handlers.py`, `infra/state_machine.json` |
| 3 | Making It Survive Monday Morning | `metrics.py`, `cost_optimizations.py`, `athena_export.py`, `infra/map_state_concurrency_snippet.json` |

## Layout

```
aws-invoice-pipeline/
├── articles/                     the three Medium articles (markdown)
├── infra/
│   ├── state_machine.json        Step Functions definition (retries, Choice gate, DLQ path)
│   └── map_state_concurrency_snippet.json   MaxConcurrency fix from the Monday incident
└── src/invoice_pipeline/
    ├── schemas.py                Pydantic v2 data contracts between stages
    ├── textract_extractor.py     async Textract + table/form parsing
    ├── llm_extractor.py          Haiku classification, Sonnet extraction (Bedrock)
    ├── validator.py              sanity checks, duplicate detection, DynamoDB write, SNS alert
    ├── handlers.py               Lambda entry points for each Step Functions state
    ├── metrics.py                CloudWatch custom metrics + review-rate alarm
    ├── cost_optimizations.py     pre-filter, model tiering, file-hash dedup
    └── athena_export.py          nightly DynamoDB -> Parquet export for Athena
```

## Setup

```bash
pip install -r requirements.txt
export AWS_DEFAULT_REGION=us-east-1
```

You need to bring: an S3 bucket, a DynamoDB table `processed-invoices` (GSIs `vendor-invoice-index` on `vendor_name`/`invoice_number` and `file-hash-index` on `file_hash`), an SNS topic, Bedrock model access for Claude Haiku and Sonnet in your region, and Lambda functions named as in `infra/state_machine.json`. Replace the placeholder account ID `123456789` and ARNs with your own.

## Status, read before deploying

- Imports cleanly and the Pydantic contracts (line-total and invoice-total checks) are exercised locally.
- Textract, Bedrock, DynamoDB, SNS and CloudWatch calls are **not** tested against a live AWS account from this repo. Treat it as a reference implementation and test on a small batch first.
- `choose_extraction_model()` in `cost_optimizations.py` is the Part 3 tiering logic; `llm_extractor.extract_invoice_data()` still calls Sonnet directly, so wire the two together if you want the Haiku-first behavior.
- No IAM policies or Terraform are included. Scope IAM to least privilege yourself.
