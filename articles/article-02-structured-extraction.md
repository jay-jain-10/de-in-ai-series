# The Unstructured Data Problem at Enterprise Scale: Extracting Structure from Chaos

**Part 2 of the "Data Engineering in the Age of AI" Series**

> **The Problem:** Traditional OCR and regex extraction fail on unstructured documents with variable formats, context window chaos, and hallucinations. Format explosion, chunking boundaries, and inconsistent document structures cause 30%+ of extractions to be unusable.
>
> **Who This Is For:** Lead data engineers and data architects building production AI pipelines on AWS/GCP
>
> **What You'll Walk Away With:** A six-stage extraction pipeline (format detection, OCR, intelligent chunking, schema validation, dead-letter queues) that achieves 95%+ accuracy on unstructured documents at scale.

I sat in a legal-tech startup's conference room last year when their VP of Product made the request: "We have 10,000 vendor contracts. They come in as PDFs. We need to extract clauses—payment terms, termination conditions, liability caps—and build a comparison dashboard. How long?"

I made the mistake of saying "two weeks." I was thinking about the API calls, the prompt tuning, maybe some retry logic. I wasn't thinking about what "10,000 PDFs" actually means.

It was six months. Here's why.

## Why "Just OCR It and Call the API" Falls Apart at Scale

The naive approach is seductive in its simplicity:

1. Loop through 10,000 PDFs
2. Extract text (OCR if scanned, direct text if digital)
3. Send chunks to Claude with prompt: "Extract payment terms"
4. Collect JSON responses
5. Load into warehouse
6. Build dashboard

You'd get results. About 70% of them would be usable. 30% would be garbage, and you wouldn't understand why until humans tried to use the system.

Here's what kills you:

**Format explosion**: The 10,000 PDFs aren't 10,000 instances of the same format. Some are digital documents where text exists as text (no OCR needed). Some are scans from 2003 at 150 DPI resolution. Some are mixed: pages 1–3 are digital, pages 4–50 are scanned. Some are PDFs generated from Word with embedded images. Some are Excel spreadsheets exported as PDF with tables that OCR destroys. A single-strategy approach fails immediately.

**Context window chaos**: A vendor contract is 30 pages. The liability cap clause you need to extract might appear on page 2. Or it might be scattered: page 2 mentions liability, page 15 defines caps, page 28 references an exhibit that explains it further. A naive chunking strategy that splits on page boundaries loses semantic coherence. You need to know what you're looking for *before* you search, which is a chicken-egg problem.

**Formatting inconsistency**: Legal teams are chaotic. One vendor puts all terms in a bulleted list. Another uses dense prose with footnotes. A third uses a 5-column table with headers that aren't part of the actual contract. Your extraction prompt has to account for all three formats. When it doesn't, you get 40% precision on "extract payment terms." That's not a tuning problem. That's an architecture problem.

**Hallucination at scale**: Send Claude a chunk that says "Party A may terminate within 30 days of written notice," and ask "what are the termination conditions?" It extracts correctly. Send it a chunk that says nothing about termination (because you chunked wrong and the termination clause ended up in the next chunk), and ask the same question. Claude might hallucinate: "The agreement specifies a 60-day notice requirement." It sounds plausible. It's wrong. At 10,000 documents, 2% hallucination rate means 200 wrong extractions. Your team doesn't discover this until weeks later when business users start comparing contracts and find inconsistencies.

The legal-tech startup discovered this the hard way. Their Sales team tried to compare two vendor contracts and found that the extraction system claimed both had "30-day termination notice." One actually said 30 days, the other said 60 days. The extraction was wrong because the document chunking split the 60-day clause across two separate API calls, and neither chunk independently mentioned termination. The system picked up "30" (the default) and missed the actual term.

That's when they realized: this isn't a data quality problem downstream. This is an *input quality* problem that no validation can fix.

## The Pipeline as a System: Six Critical Stages

The real solution isn't a script. It's a system with explicit stages, each solving a different problem. All code is on GitHub at https://github.com/jay-jain-10/de-in-ai-series.

```
┌─────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  S3     │───▶│   Format     │───▶│    Text      │───▶│  Intelligent │
│ (PDFs)  │    │  Detection   │    │  Extraction  │    │   Chunking   │
└─────────┘    │              │    │              │    │  (Haiku)     │
               │ Digital? ─┐  │    │ PyPDF2 or    │    │              │
               │ Scanned?  │  │    │ Textract/    │    │ Section      │
               │ Hybrid? ──┘  │    │ Document AI  │    │ boundaries   │
               └──────────────┘    └──────────────┘    └──────┬───────┘
                                                              │
               ┌──────────────┐    ┌──────────────┐    ┌──────▼───────┐
               │  Dead-Letter │◀───│  Validation  │◀───│  Extraction  │
               │    Queue     │    │  Layer (GE)  │    │  (Sonnet)    │
               │              │    │              │    │              │
               │ Quarantine   │    │ Schema +     │    │ Pydantic     │
               │ S3 prefix    │    │ Range +      │    │ enforced     │
               │ + error meta │    │ Hallucination│    │ JSON output  │
               └──────────────┘    └──────┬───────┘    └──────────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │              Snowflake                     │
                    │  ┌───────────────┐  ┌──────────────────┐  │
                    │  │  Extracted    │  │   Human Review    │  │
                    │  │  Contracts   │  │   Queue           │  │
                    │  │  (MERGE on   │  │   (conf < 0.7)    │  │
                    │  │  contract_id │  │                    │  │
                    │  │  + version)  │  │   SLA: 24 hours   │  │
                    │  └───────────────┘  └──────────────────┘  │
                    └───────────────────────────────────────────┘
```

**Stage 1: Format Detection**

Before you do anything else, you need to know what you're dealing with. The format detection stage reads the PDF header and analyzes the first page. Is this a digital document (text was born digital) or a scan?

If digital, it extracts text directly using PyPDF2. If scanned, it falls back to Tesseract OCR (or better, AWS Textract if you're on AWS, which has higher accuracy on complex legal documents).

But there's a subtlety: the code also checks for hybrid documents (some pages digital, some scanned). It segments the PDF page-by-page, runs format detection on each page, and routes accordingly. A 50-page contract where pages 1–30 are digital and pages 31–50 are scanned gets split into two flows: fast text extraction for the digital half, OCR for the scanned half.

The output is metadata: document_id, total_pages, pages_digital, pages_scanned, detected_language, file_size, detected_text_density (how much of each page is actually text vs. images/signatures).

**Stage 2: Text Extraction with Fallback**

For digital documents, PyPDF2 extracts raw text. It's fast and accurate when PDFs were born digital. It fails catastrophically when PDFs have embedded images or complex formatting (which PyPDF2 doesn't handle).

For scanned documents, you need OCR. Here's where the cloud choice matters:

On AWS, use Textract instead of Tesseract. Tesseract is free and open-source, but Textract (AWS's specialized OCR) is 15–20% more accurate on complex layouts and handles tables better. At $1.50 per 1000 pages, processing 10,000 contracts costs $15. Tesseract costs $0 but takes 10x longer and has 15–20% worse accuracy. The business case for Textract is clear.

On GCP, Document AI (Google's equivalent) is similarly accurate to Textract.

The extractor also handles edge cases: rotation detection (some scanned documents are upside-down), language detection (is this in English or another language?), and noise filtering (blank pages, signature pages that are just images).

The output is raw text with metadata: extraction_method (PyPDF2 or OCR), confidence_score (how certain is the OCR, if used), detected_language.

**Stage 3: Intelligent Chunking**

Raw text from a 50-page contract is useless—it's too big to fit in a context window with your extraction prompt. You need to chunk it intelligently.

The naive approach is to split on page boundaries. But this breaks clauses that span pages. The slightly better approach is to split on sentence boundaries (every 1000 tokens or so). But this still breaks multi-sentence clauses.

The correct approach: use Claude (running cheaply on Haiku) to segment the document into logical sections. The code prompts Claude: "Identify the major sections of this contract (e.g., 'Payment Terms', 'Termination', 'Liability'). Return section boundaries."

Claude identifies boundaries, and the chunker splits the text there. Now when you extract "payment terms," you're extracting from a chunk that actually contains payment-related content, not from an arbitrary page boundary.

This pre-processing step using Haiku (cheap) means your later extractions using Sonnet (expensive) work on well-segmented context. It's a trade-off: extra API calls now, but far fewer hallucinations later. The math works out—Haiku is so cheap that the improvement in accuracy saves money overall.

**Stage 4: Extraction with Schema Validation**

Now you can extract with confidence. For each clause you need to extract (payment terms, termination conditions, liability cap), the code sends a specific chunk to Claude with a Pydantic schema that enforces structure.

The code requires:
- The extracted value (e.g., "30 days")
- A confidence score (0.0–1.0)
- The page number where the value was found
- The exact text span from the original document

The Pydantic validator enforces these fields. If Claude returns JSON without a confidence score, the code re-prompts with a clearer example. This schema enforcement is non-negotiable—it's your contract with the AI.

**Stage 5: Validation Layer with Great Expectations**

Once extracted, each field is validated against business rules using Great Expectations. Does the extracted payment term match expected formats (e.g., "30 days" not "about a month")? Is the confidence score reasonable (shouldn't extract payment terms with 0.4 confidence from a contract where payment terms are explicitly stated)? Do extracted values fall within business-expected ranges (payment terms should be 15–180 days, not 500 days)?

Failed validations don't block the pipeline. They flag records for human review. A contract where the extraction confidence is below 0.7 gets routed to a review queue instead of production.

**Stage 6: Dead-Letter Queue and Human Review**

The dead-letter queue is your escape hatch. When extraction fails completely (the OCR output is unreadable, Claude returns unparseable JSON, or validation catches serious issues), the document goes to a quarantine S3 prefix with detailed error metadata.

Human reviewers process these documents manually. Their corrections are logged as ground truth, used later to validate and improve the extraction pipeline.

## Cloud Architecture: Where This Pipeline Runs

### AWS: Textract, Step Functions, and Snowflake

On AWS, here's the stack:

**Ingestion**: S3 bucket with event notifications. As PDFs land, SNS triggers a message to kick off the orchestration.

**Orchestration**: AWS Step Functions instead of Airflow. Step Functions is Amazon's serverless orchestrator. It's lower-latency than Airflow for short-lived jobs (document processing often completes in minutes), and it integrates tightly with Textract and Lambda. You define state machines (workflows) in JSON, and Step Functions handles retries, branching logic, and parallel execution natively.

Here's why Step Functions over self-hosted Airflow:
- For batch daily runs of 10K documents, Airflow's operational overhead is unnecessary
- Step Functions scales from zero—you don't pay for idle infrastructure
- State machine definitions are simple JSON; you don't maintain a Postgres database or upgrade Airflow versions
- Error handling is declarative (retry logic, catch errors, route to dead-letter queues) rather than code

**Format Detection & Extraction**: Lambda functions handle the lightweight logic (reading PDF headers, format detection). For OCR, you call AWS Textract directly. Textract is AWS's specialized service for document intelligence—it's specifically trained for PDFs, invoices, contracts, and forms.

Here's the flow:
1. Lambda reads PDF, checks first page (is it digital or scanned?)
2. If digital: Lambda calls PyPDF2 to extract text (stays in Lambda, <200ms)
3. If scanned: Lambda sends PDF to Textract, waits for response (typically 30–60 seconds)
4. Textract returns text + bounding boxes + table data + confidence scores

Textract is worth calling out because it's not just OCR. It detects tables and returns structured data (which rows, which columns, cell values). This is critical for contracts with pricing tables or payment schedules. Naive OCR would mangle these. Textract handles them.

**Chunking & Extraction**: ECS Fargate tasks (same pattern as Article 1) orchestrate Claude API calls. Lambda has a 15-minute timeout; document processing frequently takes longer. Fargate handles 30–60 minute extraction jobs gracefully. You define a Fargate task with 2 vCPU and 4GB memory. Step Functions spawns these tasks for complex documents. Simple documents complete in Lambda; complex ones scale to Fargate.

**Validation**: Lambda runs Great Expectations tests. Great Expectations is relatively lightweight (pure Python), so Lambda is cost-effective. Validation happens immediately after extraction. Failed validations route records to a quarantine table instead of blocking the pipeline.

**Dead-Letter Queue**: S3 prefix (s3://bucket/quarantine/) stores documents that fail completely. Step Functions includes error handling for unrecoverable failures (Textract couldn't OCR, Claude returned garbage, validation failed 3 times). These documents go to quarantine with detailed error metadata. A Lambda function runs daily, summarizing quarantine documents and creating tickets for manual review.

**Warehouse**: Snowflake with idempotent MERGE on (contract_id, extraction_version). This pattern is critical: if you reprocess contracts with a new prompt or improved extraction logic, the MERGE updates existing rows rather than creating duplicates. The extraction_version column tells you which extraction logic produced each value.

**Cost for 10K contracts/quarter (one-time):**
- Textract: 10,000 contracts × 50 pages average × $1.50 per 1000 pages = $750
- Claude Haiku (segmentation): 10,000 × 5000 avg tokens × $0.80 / 1M = $40
- Claude Sonnet (extraction): 10,000 × 3 clauses × 1000 tokens per extraction × $3 / 1M = $90
- Lambda (format detection, validation, queue processing): ~$50
- Fargate (chunking, extraction): ~$200
- Step Functions (orchestration): ~$10 (pricing: $0.000025 per state transition)
- Snowflake: ~$50
- CloudWatch (logs + metrics): ~$20

**Total: ~$1210/quarter (~$0.121 per contract)**

### GCP: Document AI, Cloud Workflows, BigQuery

On GCP:

**Ingestion**: GCS bucket with Pub/Sub notifications.

**Orchestration**: Cloud Workflows (Google's Step Functions equivalent) or Cloud Composer (managed Airflow, if you prefer that operational model).

**Format Detection & Text Extraction**: Document AI (Google's OCR/document intelligence service). Document AI is more expensive than Textract per document ($2–3 depending on processor vs. $1.50 flat for Textract), but it's more specialized. Google trained Document AI on a broader range of business documents—invoices, receipts, contracts, forms. It understands semantic structure (it knows "this is the parties clause, this is the payment terms").

On AWS, you send a PDF to Textract and get back OCR text. On GCP, you send a PDF to Document AI and get back structured data: entities (names, dates, amounts) already extracted.

**Chunking & Extraction**: Cloud Run for Claude API calls. Cloud Run auto-scales based on concurrent requests. You only pay for the time your code is running. If processing 10K documents takes 4 hours, you pay for 4 hours of compute, not 24/7 base cost. This is more cost-efficient than always-on Fargate for variable workloads.

**Validation**: Cloud Functions (lightweight, serverless) for Great Expectations tests.

**Warehouse**: BigQuery or Snowflake. BigQuery's streaming insert API (via Dataflow) makes real-time updates easier than Snowflake's batch loading.

**Cost**: Roughly equivalent to AWS (~$0.10–0.15 per contract, depending on Document AI processor choice and extraction complexity).

### AWS Textract vs. Open-Source Tesseract: The Trade-Off Explained

This deserves detailed analysis because it's the first real infrastructure decision you make:

**Tesseract (open-source)**:
- Cost: $0 (but requires compute infrastructure)
- Accuracy: ~85–90% on clean scans, 50–70% on complex layouts
- Latency: ~1–2 minutes per page
- Setup: Run on Lambda or container; you own infrastructure

**AWS Textract**:
- Cost: $1.50 per 1000 pages
- Accuracy: ~95–98% on clean scans, 85–90% on complex layouts
- Latency: ~10–30 seconds per page (API call + inference)
- Setup: Managed service, no infrastructure to maintain

For the legal-tech startup with 10,000 contracts:

**Tesseract path**:
- 10,000 contracts × 50 pages = 500,000 pages
- Tesseract at 1–2 min per page = 500K–1M minutes = 8K–16K compute-hours
- Lambda pricing: 1 GB memory, $0.0000166667 per second = ~$0.06 per compute-hour
- Total: ~$500–950/quarter in compute
- Plus: 30% of extractions fail or are wrong (hallucinations from poor OCR)
- Human review labor: 150K pages × 30% × 5 min per review × $50/hr = ~$12,500/quarter
- **Total: ~$13K–14K/quarter**

**Textract path**:
- 500,000 pages × $1.50 / 1000 = $750
- Infrastructure (Lambda, Step Functions, Snowflake): ~$300
- Only 5% of extractions need human review
- Human review labor: 150K pages × 5% × 5 min × $50/hr = ~$200/quarter
- **Total: ~$1.25K/quarter**

The Textract path is 10x cheaper because accuracy matters. This pattern repeats across AI infrastructure: managed services cost more per unit, but the operational simplicity and accuracy justify it at scale. The startup chose Textract immediately.

### Why This Matters: The Total Cost of Ownership

Many engineers see $1.50 per 1000 pages and think Textract is expensive. They don't account for the human review cost. At enterprise scale, labor dominates the budget. Improving accuracy by 20% saves more money than reducing API costs by 50%.

## Trust Boundaries and Dead-Letter Queues

Here's a critical architectural pattern: where do you trust the AI output, and where do you require human validation?

The legal-tech startup implemented this pyramid:

**High confidence (0.95+)**: Direct to production tables. These are presented to users without flags.

**Medium confidence (0.75–0.95)**: Production tables, but flagged for optional review. Sales can use them, but they see a badge saying "Human validation recommended."

**Low confidence (0.5–0.75)**: Quarantine. Not served to users; requires manual review before production.

**Extraction failures (confidence 0)**: Dead-letter queue. Someone needs to manually extract these clauses.

This isn't paranoia. It's acknowledgment that AI extraction is probabilistic, and at scale, you need clear boundaries about what's trustworthy and what isn't.

## Data Quality Testing in an Extraction Pipeline

Your tests change fundamentally:

Traditional data quality tests: "All rows have a non-null payment_terms field" (check, count matches expected).

Extraction pipeline tests:

- **Schema conformance**: Does the extracted JSON match the Pydantic schema? (Yes/no check)
- **Value range tests**: Is the extracted payment term between 15 and 180 days? (catch "500 days" hallucinations)
- **Format consistency**: Does the extracted value match expected regex patterns? (payment terms should match "\\d+ (days|months)")
- **Confidence distribution**: Is the median confidence > 0.75? (early warning for accuracy degradation)
- **Hallucination detection**: Does the extracted value appear textually in the source document? (check if extracted "30 days" exists in original text)

The last test is subtle but powerful. When extraction confidence is high but the extracted value doesn't appear in the original document, you've found a hallucination. Flag it for review.

## Cost Analysis for Legal-Tech

For 10,000 contracts/quarter:

**If using Textract**:
- Textract: 10,000 contracts × 50 pages average × $1.50 / 1000 = $750
- Claude Haiku (pre-segmentation): 10,000 × 5000 avg tokens × $0.80 / 1M = $40
- Claude Sonnet (extraction): 10,000 × 3 clauses × 1000 tokens per extraction × $3 / 1M = $90
- Infrastructure (Step Functions, Lambda, Fargate, Snowflake): ~$300
- **Total: ~$1180/quarter (~$0.118 per contract)**

**If using Tesseract**:
- Tesseract infrastructure: ~$150/month = $450/quarter
- Claude Haiku: $40
- Claude Sonnet: $90
- Infrastructure: $300
- Human review labor (30% error rate, 5 minutes per contract, $50/hr): 10,000 × 0.30 × 5 min = 25,000 minutes = ~$21,000
- **Total: ~$22,000/quarter (~$2.20 per contract)**

The $1 difference in per-contract cost doesn't capture the reality: Textract eliminates most manual review work. The startup's decision was instant.

## Data Engineering Fundamentals: Extraction Pipeline Patterns

Building a production extraction pipeline isn't just about calling Claude APIs—it's about applying core data engineering principles to the unstructured domain. Here are the patterns that separate prototype projects from enterprise systems:

### Idempotency: The MERGE Pattern

The extraction pipeline stores results in Snowflake using idempotent MERGE statements:

```sql
MERGE INTO extracted_contracts AS target
USING extracted_contracts_staging AS source
ON target.contract_id = source.contract_id
  AND target.extraction_version = source.extraction_version
WHEN MATCHED THEN
  UPDATE SET
    payment_terms = source.payment_terms,
    termination_conditions = source.termination_conditions,
    liability_cap = source.liability_cap,
    last_updated = CURRENT_TIMESTAMP
WHEN NOT MATCHED THEN
  INSERT (contract_id, extraction_version, payment_terms, ..., created_timestamp)
  VALUES (...)
```

This pattern ensures that re-running the pipeline doesn't create duplicates. If your extraction logic improves and you decide to reprocess all contracts with a new prompt, the MERGE updates existing rows in place, keyed on (contract_id, extraction_version). You maintain a clean audit trail: each extraction_version row represents what the pipeline could extract with that specific logic.

### Schema Evolution: Graceful Field Addition

Legal contracts demand new extractions over time. Six months in, stakeholders ask: "Can you also extract governing_law?"

Without schema evolution, this breaks your pipeline. With it, you add the field to the Pydantic schema:

```python
class ContractExtraction(BaseModel):
    payment_terms: str
    termination_conditions: str
    liability_cap: str
    governing_law: str  # New field
    confidence_scores: dict
    extraction_version: float
```

Old extracted records have NULL for governing_law. New records have it populated. Downstream dbt models handle both using COALESCE:

```sql
SELECT
  contract_id,
  COALESCE(governing_law, 'Not extracted') as governing_law
FROM extracted_contracts
WHERE extraction_version >= 2.0
```

This allows you to evolve the schema without backfilling or pipeline rewrites.

### Backfill: Selective Reprocessing

The extraction_version column enables targeted backfills. When you fix a critical bug in your extraction prompt or upgrade to a new Claude model, you don't reprocess everything—only what's necessary:

```sql
SELECT contract_id
FROM extracted_contracts
WHERE extraction_version < 2.0
  AND contract_date > '2024-01-01'
```

This query identifies 500 contracts that need reprocessing. You route only these back through the pipeline. The MERGE handles updates. The rest of your 10,000 contracts remain untouched, preserving their original extraction history.

### Data Lineage: Metadata That Explains

Each extracted record carries lineage metadata:

- **extraction_method**: PyPDF2 vs. Textract (which path did this document take?)
- **ocr_confidence**: 0.95 (how certain is the OCR on scanned pages?)
- **extraction_version**: 2.1 (which extraction logic produced this value?)
- **prompt_version**: "claude-extraction-v3" (which prompt did we use?)
- **model_name**: "claude-3-5-sonnet-20241022" (which Claude model?)
- **processing_timestamp**: 2024-03-12T14:32:15Z (when did this happen?)

Three months later, a user asks: "Why does contract X show payment_terms='30 days' when I think it said 60 days?"

You query the lineage: extraction_version=1.5, prompt_version="claude-extraction-v1", model_name="claude-3-sonnet-20240229". You see that this extraction happened before you fixed the prompt to handle multi-page clauses correctly. You reprocess this contract with extraction_version=2.0, and the MERGE updates it to the correct value.

Lineage answers the eternal question: "Why is this value wrong?" months after extraction.

### Dead-Letter Queue as an Architectural Pattern

The quarantine S3 prefix (s3://bucket/quarantine/) isn't just error handling—it's a critical architectural pattern. Documents that fail completely go there with full error context:

```json
{
  "contract_id": "vendor-acme-2024-001",
  "s3_key": "s3://bucket/contracts/vendor-acme-2024-001.pdf",
  "failure_stage": "ocr",
  "error_message": "Textract confidence < 0.5 on all pages",
  "ocr_confidence_scores": [0.42, 0.38, 0.51, 0.45],
  "processing_timestamp": "2024-03-12T14:32:15Z",
  "attempt_count": 3,
  "last_attempt_model": "claude-3-5-sonnet-20241022"
}
```

This metadata enables four critical operations:

1. **Root cause analysis**: Is the document genuinely unreadable, or is it a specific document type Textract struggles with?
2. **Reprocessing when bugs are fixed**: If you discover that your extraction prompt was hallucinating termination conditions, you can reprocess the dead-letter queue with the new prompt.
3. **Training data for pipeline improvement**: Documents in quarantine are your hardest cases. Manually extracting them and storing as ground truth trains your next generation of prompts.
4. **SLA tracking**: You can measure: "Of the 50 documents in quarantine, how many were resolved within 24 hours?" This feeds into operational metrics.

The pattern: failures aren't black holes—they're data points that inform the next iteration.

### SLAs: From Ad-Hoc to Quantified

The extraction pipeline enforces three SLAs, all tracked in Step Functions:

**Extraction Pipeline SLA**: 95% of contracts processed within 4 hours
- This covers: format detection, OCR, chunking, extraction, validation
- You measure: P95 latency from S3 upload to Snowflake MERGE
- Violation: Alert the ops team. Is Step Functions overwhelmed? Are Textract APIs degraded?

**Human Review SLA**: Quarantined records reviewed within 24 hours
- When a contract hits the quarantine prefix or confidence < 0.7, a Lambda function creates a task in your review system
- You measure: Time from quarantine to human review completion
- Violation: Escalate to the VP of Product. Your extraction quality is degrading, and humans can't keep up

**Feedback Loop SLA**: Ground truth from human reviews integrated into training data within 1 week
- When a human corrects an extraction (e.g., "This should be 60 days, not 30"), that correction is logged
- You measure: Lag between human correction and inclusion in your training dataset for the next prompt iteration
- Violation: Your pipeline is learning slowly. Consider increasing review prioritization or prompt refinement cadence

These SLAs transform extraction from a best-effort process ("We process contracts, hope they're good") into a measurable system ("95% of contracts are processed within 4 hours, and if they're not, we know why").

## Skills Gained

Building this pipeline teaches:

- **Document intelligence patterns**: Format detection, OCR fallbacks, intelligent chunking
- **Confidence-based routing**: Splitting AI output by confidence, not binary pass/fail
- **Validation and dead-letter handling**: Testing non-deterministic extractions, handling failures gracefully
- **Multi-stage orchestration**: Designing pipelines where each stage has different latency/cost/accuracy characteristics
- **Trade-offs between accuracy and cost**: When to use Haiku vs. Sonnet, Tesseract vs. Textract

## What's Next

We've solved extraction. But you have 10,000 extracted contracts, and nobody knows if the extraction quality is degrading. Did last month's batch have better extraction accuracy than this month? How do you detect prompt drift when your extraction logic changes?

That's Part 3: Prompt Governance Is the New Schema Governance.

---

## Code & Resources

**GitHub Repository:** [github.com/jay-jain-10/de-in-ai-series](https://github.com/jay-jain-10/de-in-ai-series)

**What this article covers:** A six-stage extraction pipeline (format detection, OCR, intelligent chunking, schema validation, dead-letter queues) that turns 10K unstructured PDFs into structured contract data using Textract + Claude with 95%+ accuracy.

**What's in the repo:**
- `articles/` — All 8 articles in this series as markdown, each with architecture diagrams, AWS/GCP cost breakdowns, trade-off analyses, and DE fundamentals sections
- `README.md` — Series overview with a summary table showing what problem each article solves and the key architecture pattern

**Series reading order:** This is Part 2 of 8. Article 1 introduced AI as a transformation layer. This article tackles unstructured data extraction at scale and introduces dead-letter queues and human review workflows. Next: Article 3 tackles prompt governance and CI/CD for AI systems. Read the full series overview in the [README](https://github.com/jay-jain-10/de-in-ai-series).
