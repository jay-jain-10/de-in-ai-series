# Data Engineering in the Age of AI

**An 8-part series for lead data engineers and data architects who need to build production AI pipelines — not toy demos.**

---

## What Problem Does This Series Solve?

Most "AI pipeline" tutorials show you how to call an API. That's the easy part. The hard part is everything else: What happens when your AI model silently drifts from 94% to 71% accuracy and nobody notices? How do you handle non-deterministic outputs in a pipeline that expects consistency? How do you justify $47K/month in API costs to your CFO when the budget is $5K?

This series addresses the **architectural gap** between "I can call Claude/GPT from Python" and "I can ship a production AI pipeline that handles failure, cost, governance, and scale on AWS/GCP."

Each article tackles a real scenario — fintech ticket classification, legal document extraction, fraud detection, claims processing — and walks through the architecture decisions a 10+ YOE lead DE would make, including the trade-offs, the cost breakdowns, and the things that will break at 2 AM.

---

## What's in This Repository?

```
de-in-ai-series/
├── README.md                                    ← You are here
└── articles/
    ├── article-01-ai-native-pipeline.md         ← AI as a transformation layer (fintech, 50K tickets)
    ├── article-02-structured-extraction.md      ← Document intelligence at scale (legal-tech, 10K contracts)
    ├── article-03-prompt-engineering.md          ← Prompt governance as schema governance (CI/CD for prompts)
    ├── article-04-multi-model-orchestration.md   ← Router, chain, fan-out, fallback patterns (e-commerce reviews)
    ├── article-05-realtime-ai-streams.md        ← Streaming + AI latency budgets (fraud detection)
    ├── article-06-ai-data-quality.md            ← Semantic quality layer (healthcare validation)
    ├── article-07-cost-engineering.md           ← FinOps for AI pipelines (89.5% cost reduction)
    └── article-08-capstone.md                   ← Platform architecture (insurance claims automation)
```

Each article is a **self-contained architectural reference** containing:

- **ASCII architecture diagrams** showing data flow and infrastructure layout
- **AWS and GCP service recommendations** with specific services, WHY each one, and per-service monthly costs
- **Trade-off analysis** (e.g., Textract vs Tesseract, Kafka vs Pub/Sub, Haiku vs Sonnet)
- **DE fundamentals** applied to AI: idempotency, exactly-once semantics, data contracts, SLAs, lineage, backfill
- **Worked examples** with real numbers (cost breakdowns, latency profiles, accuracy thresholds)
- **"When NOT to use" guidance** — every pattern has anti-patterns

---

## The Series at a Glance

| # | Article | Problem It Solves | Key Architecture Pattern |
|---|---------|------------------|------------------------|
| 1 | **The AI-Native Data Pipeline** | Traditional ETL breaks when AI is the transform (non-deterministic, silent failures, cost variability) | 5-stage pipeline: Ingest → Validate → AI Transform → Route by Confidence → Load |
| 2 | **Structured Extraction at Scale** | OCR/regex can't handle variable document formats at enterprise scale | 6-stage extraction: Format Detection → Text Extraction → Chunking → AI Extraction → Validation → Load |
| 3 | **Prompt Governance** | Prompt changes silently break production pipelines — no versioning, no testing, no rollback | Prompt CI/CD pipeline: Git → Golden Dataset Tests → S3 Registry → Production Monitoring |
| 4 | **Multi-Model Orchestration** | Single models can't optimize cost, latency, AND accuracy for diverse tasks simultaneously | 4 patterns: Router, Chain, Fan-Out/Fan-In, Fallback Cascade with YAML-config routing |
| 5 | **Real-Time AI Streams** | AI inference latency (200ms-2s) destroys streaming's predictable latency model (<10ms) | Hybrid architecture: Critical path (rule-based, 8ms) + Async enrichment (AI, 0.2-2.2s) |
| 6 | **Semantic Data Quality** | Rule-based quality checks plateau at 60% — they catch syntax errors but miss semantic incoherence | Two-layer validation: Syntactic (100% rule-based) → Semantic (AI-sampled) with human feedback loop |
| 7 | **FinOps for AI Pipelines** | AI API costs kill projects before they ship ($47K/month actual vs $5K budget) | 4 compounding levers: Caching (40%) → Model Tiering (53%) → Prompt Optimization (25%) → Batching (50%) = 89.5% reduction |
| 8 | **From Pipeline to Platform** | Building 4 separate AI pipelines = duplication, inconsistency, wasted effort | AI Gateway + shared services: Ingestion → Processing → Gateway → Validation → Orchestration → Warehouse |

---

## Who This Is For

**Lead data engineers and data architects** (5-10+ YOE) building production AI pipelines on AWS or GCP. You already know Airflow, dbt, Snowflake, and Spark. You need the architectural patterns for when AI becomes a first-class component in your data platform — not a side project.

You'll get the most value if you're facing questions like:

- "How do I handle non-deterministic outputs in a pipeline that feeds dashboards?"
- "My AI pipeline works in dev but costs 10x what we budgeted in production"
- "We're calling Claude from 4 different services with no consistency or governance"
- "Rule-based data quality catches 60% of issues — what catches the rest?"

## Tech Stack Referenced

- **AI Models:** Claude API (Haiku for high-volume/low-cost, Sonnet for complex tasks, Opus for quality-critical)
- **Orchestration:** Apache Airflow (MWAA on AWS / Cloud Composer on GCP), Dagster
- **Cloud:** AWS (MWAA, ECS Fargate, Lambda, S3, Step Functions, MSK, ElastiCache, Bedrock) and GCP (Cloud Composer, Cloud Run, GCS, Pub/Sub, Memorystore, Vertex AI, BigQuery)
- **Warehouse:** Snowflake, BigQuery
- **Transformation:** dbt
- **Streaming:** Kafka / Confluent, AWS MSK, GCP Pub/Sub
- **Quality:** Great Expectations, Pydantic (as data contracts)
- **Language:** Python 3.10+

## How to Use This Series

1. **Read in order** — each article builds on concepts from the previous one
2. **Use the architecture diagrams** as starting points for your own designs
3. **Adapt the cost models** — swap in your cloud provider, your volume, your model choice
4. **Fork this repo** and customize the patterns for your environment

---

## License

MIT
