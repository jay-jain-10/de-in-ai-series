# Data Engineering in the Age of AI

**Building Real-World AI Pipelines for the Modern Data Engineer**

An 8-part Medium series exploring how AI is reshaping data engineering — from architecture patterns to platform thinking. Each article covers the *why* and *when*; the code here covers the *how*.

---

## The Series

| # | Article | Architectural Focus | Code |
|---|---------|-------------------|------|
| 1 | **The AI-Native Data Pipeline** | Why the transformation layer is being rewritten | [article-01](./article-01-ai-native-pipeline/) |
| 2 | **The Unstructured Data Problem at Enterprise Scale** | Document intelligence as system design | *coming soon* |
| 3 | **Prompt Governance Is the New Schema Governance** | Prompts as production infrastructure | *coming soon* |
| 4 | **Designing for Model Heterogeneity** | Router, chain, fan-out, and fallback patterns | *coming soon* |
| 5 | **Where AI Meets Event-Driven Architecture** | Latency budgets and streaming trade-offs | *coming soon* |
| 6 | **The Semantic Data Quality Layer** | Why rule-based DQ plateaus at 60% | *coming soon* |
| 7 | **FinOps for AI Pipelines** | Unit economics that ship or kill your project | *coming soon* |
| 8 | **From Pipeline to Platform** | Building an AI data platform for your org | *coming soon* |

## Who This Is For

Mid-to-senior data engineers (2-5+ years) who want to integrate AI into their pipelines at an architectural level — not just call an API, but design systems that handle failure, cost, governance, and scale.

## Tech Stack

- **AI:** Claude API (Haiku / Sonnet / Opus), Anthropic Python SDK
- **Orchestration:** Apache Airflow, Dagster
- **Warehouse:** Snowflake
- **Transformation:** dbt
- **Storage:** AWS S3, Redis
- **Streaming:** Kafka / Confluent
- **Quality:** Great Expectations, Pydantic
- **Language:** Python 3.10+

## Getting Started

```bash
cd article-01-ai-native-pipeline
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

## License

MIT
