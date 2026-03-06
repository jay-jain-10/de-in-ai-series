# Data Engineering in the Age of AI

**Building Real-World AI Pipelines for the Modern Data Engineer**

An 8-part Medium article series with production-ready code for every project.

---

## Series Overview

The data engineering landscape has shifted. AI models like Claude are no longer edge experiments — they're core pipeline components. This series teaches mid-level data engineers how to design, build, and deploy AI-native data pipelines through hands-on projects.

## Articles

| # | Title | Focus | Code |
|---|-------|-------|------|
| 1 | **The AI-Native Data Pipeline** | AI as a transformation stage in ETL | [article-01](./article-01-ai-native-pipeline/) |
| 2 | **Structured Data Extraction at Scale** | PDF → structured tables with Claude | *coming soon* |
| 3 | **Prompt Engineering Is the New SQL** | Version-controlled, testable prompts | *coming soon* |
| 4 | **Multi-Model Orchestration Patterns** | Haiku/Sonnet/Opus routing & fallbacks | *coming soon* |
| 5 | **Real-Time AI Streams** | Kafka + Claude for streaming anomaly detection | *coming soon* |
| 6 | **AI-Powered Data Quality** | Semantic validation beyond rule-based checks | *coming soon* |
| 7 | **Cost Engineering for AI Pipelines** | Cutting API costs by 90% | *coming soon* |
| 8 | **Capstone: End-to-End AI Data Platform** | Insurance claims processing pipeline | *coming soon* |

## Target Audience

Mid-level data engineers (2–5 years experience) who want to integrate AI into their pipelines — not as a side project, but as a first-class component alongside dbt, Airflow, and Snowflake.

## Tech Stack

- **AI:** Claude API (Haiku, Sonnet, Opus), Anthropic Python SDK
- **Orchestration:** Apache Airflow, Dagster
- **Warehouse:** Snowflake
- **Transformation:** dbt
- **Storage:** AWS S3, Redis
- **Streaming:** Kafka/Confluent
- **Quality:** Great Expectations, Pydantic
- **Language:** Python 3.10+

## Getting Started

Each article directory is a standalone project with its own README, requirements, and `.env.example`.

```bash
cd article-01-ai-native-pipeline
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

## License

MIT
