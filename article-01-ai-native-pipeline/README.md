# Article 1: The AI-Native Data Pipeline

**Why Data Engineering Is No Longer Just ETL — And What to Do About It**

Part 1 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

A production-ready pipeline that ingests customer support tickets from S3, classifies them using Claude Haiku (sentiment, category, escalation risk), and loads structured results into Snowflake for BI consumption.

## Architecture

```
S3 (JSON) → Airflow (orchestrate) → Claude Haiku (classify) → Snowflake (load) → dbt (transform) → BI Dashboard
```

## Tech Stack

| Component | Tool |
|-----------|------|
| AI Model | Claude Haiku (claude-haiku-4-5-20251001) |
| Orchestration | Apache Airflow |
| Storage | AWS S3 |
| Warehouse | Snowflake |
| Transformation | dbt |
| Language | Python 3.10+ |

## Project Structure

```
article-01-ai-native-pipeline/
├── src/
│   ├── classifier.py          # AI classification with retry logic
│   ├── batch_processor.py     # Concurrent batch processing
│   ├── snowflake_loader.py    # Snowflake MERGE loader
│   └── config.py              # Configuration management
├── dags/
│   └── ticket_classification_dag.py   # Airflow DAG
├── dbt_models/
│   ├── staging/
│   │   └── stg_ticket_classifications.sql
│   └── marts/
│       ├── fct_ticket_sentiment.sql
│       └── fct_escalation_alerts.sql
├── tests/
│   ├── test_classifier.py
│   └── test_batch_processor.py
├── .env.example
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set environment variables
```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Run locally (without Airflow)
```python
from src.classifier import TicketClassifier
from src.batch_processor import BatchProcessor

classifier = TicketClassifier(api_key="your-key")
processor = BatchProcessor(classifier)

tickets = [
    {
        "ticket_id": "TKT-001",
        "subject": "Refund not processed",
        "body": "I've been waiting 3 weeks for my refund...",
        "channel": "email",
        "created_at": "2026-02-15T14:32:00Z",
        "customer_id": "CUST-001"
    }
]

results = processor.process_tickets(tickets)
print(results)
```

## Cost Estimate

| Metric | Value |
|--------|-------|
| Avg input tokens/ticket | ~250 |
| Avg output tokens/ticket | ~80 |
| Monthly volume | 50,000 tickets |
| **Monthly cost** | **~$32.50** |

## Key Concepts

- **AI as a pipeline component**: Claude Haiku is treated as a transformation stage, not a standalone tool
- **Graceful degradation**: Failed classifications return safe defaults with `confidence=0.0`
- **Cost tracking**: Every API call logs token usage for observability
- **Idempotent loading**: Snowflake MERGE prevents duplicates on reruns
- **Data contracts**: Pydantic models enforce schema between AI output and warehouse
