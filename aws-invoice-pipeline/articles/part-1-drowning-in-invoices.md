# We Were Drowning in Invoices

*Part 1 of 3: AI + Data Engineering on AWS*

---

## Problem Statement

Last year, our finance team at a mid-size logistics company was processing around 4,000 vendor invoices a month. Manually. Three full-time people opening PDFs, squinting at scanned documents, typing numbers into SAP. They were making mistakes — not because they were bad at their jobs, but because humans aren't built to read 4,000 invoices without zoning out.

The error rate was hovering around 8%. That doesn't sound terrible until you do the math: 320 invoices per month with wrong amounts, wrong vendor codes, or missed line items. Some of those errors turned into duplicate payments. Others turned into late payment penalties. The CFO estimated we were leaking about $180K a year from invoice processing errors alone.

We tried an off-the-shelf OCR solution. It worked great on clean, typed invoices. It fell apart on handwritten notes, invoices with logos overlapping text, multi-page invoices where page 2 had different formatting than page 1. The accuracy was around 72% — which meant we still needed humans to verify almost everything. We'd spent $40K on software that barely reduced the workload.

That's when I got pulled in. "Can you build something with AI that actually works?"

## What You'll Get From This Article

I'm going to walk you through the system we designed and shipped. By the end of this three-part series, you'll have:

- A complete AWS architecture that processes invoices end-to-end — from a PDF landing in S3 to structured data sitting in your database, ready for your ERP
- Working Python code you can actually run (Part 2)
- Real cost numbers — what we spent, what we optimized, and where we messed up (Part 3)
- The tradeoffs we made, the ones we got right, and the one that cost us two weeks of rework

This first part covers the problem, the system design, and the AWS services we picked (and why).

---

## The Business Case (Because Your CTO Will Ask)

Before I drew a single architecture diagram, I had to answer one question: is this worth building?

Here's the napkin math I showed our CTO:

**Current state:**
- 3 FTEs processing invoices at ~$55K/year each = $165K/year in labor
- $180K/year in errors (duplicate payments, late fees, wrong amounts)
- Total cost of the problem: ~$345K/year

**Proposed state:**
- AI pipeline handles 85% of invoices automatically (the clean, standard ones)
- Humans handle the remaining 15% (handwritten, weird formats, edge cases)
- Target error rate: under 2%

**Expected savings:**
- Reduce from 3 FTEs to 1 FTE for review = $110K/year saved
- Reduce error leakage from $180K to ~$35K = $145K/year saved
- Pipeline cost (spoiler from Part 3): ~$150/month = $1.8K/year

**Net savings: ~$253K/year.** Payback period: about 6 weeks of engineering time.

The CTO said yes before I finished the slide.

## The System Design

Here's what we built:

```
                        ┌─────────────────────────────────────────────┐
                        │              AWS Account                     │
                        │                                              │
┌──────────┐   upload   │  ┌─────┐  event  ┌────────────┐             │
│  Vendor  │───────────▶│  │ S3  │────────▶│  EventBridge│             │
│  Portal  │            │  │     │         │  Rule       │             │
└──────────┘            │  └─────┘         └─────┬──────┘             │
                        │                        │                     │
┌──────────┐   email    │                        ▼                     │
│  Email   │───────────▶│              ┌─────────────────┐            │
│  (SES)   │            │              │  Step Functions  │            │
└──────────┘            │              │  State Machine   │            │
                        │              │                  │            │
                        │              │  ┌────────────┐  │            │
                        │              │  │ 1. Textract│  │            │
                        │              │  │  (extract) │  │            │
                        │              │  └─────┬──────┘  │            │
                        │              │        ▼         │            │
                        │              │  ┌────────────┐  │            │
                        │              │  │ 2. Bedrock │  │            │
                        │              │  │  Claude    │  │            │
                        │              │  │ (classify) │  │            │
                        │              │  └─────┬──────┘  │            │
                        │              │        ▼         │            │
                        │              │  ┌────────────┐  │            │
                        │              │  │ 3. Validate│  │            │
                        │              │  │  (Lambda)  │  │            │
                        │              │  └─────┬──────┘  │            │
                        │              │        │         │            │
                        │              │   ┌────┴────┐    │            │
                        │              │   ▼         ▼    │            │
                        │              │ ┌─────┐ ┌─────┐  │            │
                        │              │ │ OK  │ │Flag │  │            │
                        │              │ │     │ │     │  │            │
                        │              │ └──┬──┘ └──┬──┘  │            │
                        │              └────┼───────┼─────┘            │
                        │                   ▼       ▼                  │
                        │              ┌────────┐ ┌────────┐          │
                        │              │DynamoDB│ │  SNS   │          │
                        │              │(store) │ │(alert) │          │
                        │              └────────┘ └────────┘          │
                        └─────────────────────────────────────────────┘
```

Let me explain each piece and — more importantly — why we chose it over the alternatives.

## AWS Services: What We Picked and Why

### S3 — The Landing Zone

Every invoice starts here. Vendors upload through a portal, or our finance team forwards emails that get parsed by SES and dumped into S3. Nothing fancy. S3 is the obvious choice because everything downstream plugs into it natively.

One decision that saved us pain later: we structured the bucket with prefixes.

```
invoices-raw/
  ├── 2024/01/15/
  │   ├── vendor-acme-inv-4521.pdf
  │   ├── vendor-globex-inv-892.pdf
  │   └── vendor-initech-inv-1337.pdf
  └── 2024/01/16/
      └── ...
```

Date-partitioned. Sounds basic, but when you need to reprocess last Tuesday's invoices because you changed your extraction prompt, you'll thank yourself.

### EventBridge — Not S3 Events, and Here's Why

Most tutorials tell you to use S3 event notifications to trigger Lambda. We started there. It works fine until you need to add a second consumer — maybe you want to log every upload to a metrics pipeline. S3 notifications only support one destination per event type.

EventBridge solves this. S3 sends events to EventBridge, and from there you can fan out to Step Functions, Lambda, SQS, whatever. We added a metrics consumer three weeks after launch with zero changes to the existing pipeline. That flexibility was worth the extra 5 minutes of setup.

Cost: EventBridge charges $1 per million events. At 4,000 invoices/month, that's effectively free.

### Step Functions — The Orchestrator

This was the most debated decision on the team. I wanted Airflow (MWAA). Our senior engineer wanted "just chain some Lambdas." We settled on Step Functions, and it was the right call. Here's why.

Airflow is powerful but expensive for this workload. MWAA starts at ~$200/month for the smallest environment. We're processing 4,000 invoices, not 4 million. That's like renting a warehouse to store a bookshelf.

Chaining Lambdas with direct invocation is fragile. If Lambda B fails, Lambda A doesn't know. You end up building your own retry logic, state tracking, and error handling. You're basically rebuilding Step Functions poorly.

Step Functions gives us:
- Visual debugging (you can literally see which step failed and click on it)
- Built-in retries with exponential backoff
- A state machine that tracks every invoice's journey
- Pay per state transition — at 4,000 invoices × ~5 transitions each, that's 20,000 transitions/month = $0.50/month

Fifty cents. I'm not kidding.

### Textract — First Pass Extraction

Textract reads the PDF and gives us raw text plus table structures. We use the `AnalyzeDocument` API for single-page invoices and `StartDocumentAnalysis` for multi-page ones.

Why Textract and not just send the PDF straight to Claude? Cost. Textract costs $1.50 per 1,000 pages. Claude (on Bedrock) costs roughly $0.003 per 1K input tokens — and a single invoice page converts to about 800-1,200 tokens of text. So for extraction alone, Textract is cheaper AND gives us structured table data that Claude would need extra prompting to handle.

We use Textract for the mechanical work (read the text, find the tables) and Claude for the intelligent work (understand what the text means, classify it, handle ambiguity).

### Bedrock (Claude) — The Brain

Claude on Bedrock, not the direct Anthropic API. This matters for a couple of reasons:

First, Bedrock runs inside your VPC. The invoice data never leaves AWS. For a company processing vendor contracts and payment amounts, that's not a nice-to-have — it's a compliance requirement.

Second, Bedrock handles throttling and scaling. When we dump 200 invoices at 8 AM on a Monday, Bedrock manages the queue. With the direct API, we'd need to build our own rate limiter.

We use Claude Haiku for classification (cheap, fast, good enough for "is this an invoice, credit note, or purchase order?") and Claude Sonnet for extraction validation (more expensive, but catches errors Haiku misses on complex invoices).

Cost: Haiku on Bedrock is dirt cheap — fractions of a cent per invoice. For 4,000 invoices averaging 1,000 input tokens and 200 output tokens each: about $5.50/month for classification. Sonnet only runs on ~600 flagged invoices for extraction validation: roughly $8/month.

### DynamoDB — The Store

Extracted invoice data lands in DynamoDB. Not RDS, not Redshift. Here's the reasoning:

Our access pattern is simple: write once, read by invoice ID, occasionally query by vendor or date range. DynamoDB handles this with single-digit millisecond latency and zero operational overhead. No connection pooling, no vacuuming, no "the database is slow because someone ran a full table scan."

On-demand pricing at our volume: ~$3/month. If we hit 40,000 invoices/month someday, we'll revisit. Until then, DynamoDB is the laziest correct choice.

### SNS — The Alert System

When validation fails — confidence below threshold, amounts that don't add up, vendor not in our system — SNS sends an alert to Slack (via a Lambda subscriber) and adds the invoice to a human review queue.

We debated SQS vs SNS. SNS won because we needed fan-out: the alert goes to Slack AND to the review queue AND to a metrics counter. SNS does fan-out natively.

## The Cost Breakdown

Here's what this actually costs at 4,000 invoices/month:

| Service | Monthly Cost | Notes |
|---------|-------------|-------|
| S3 | $0.12 | 4K PDFs, ~2MB average |
| EventBridge | ~$0.01 | 4K events |
| Step Functions | $0.50 | ~20K transitions |
| Textract | $6.00 | 4K pages |
| Bedrock (Haiku) | $5.50 | Classification |
| Bedrock (Sonnet) | $8.00 | Validation on flagged |
| Lambda | $1.20 | Validation + alerting |
| DynamoDB | $3.00 | On-demand writes/reads |
| SNS | $0.03 | Notifications |
| **Total** | **~$24/month** | |

Twenty-four dollars a month. The coffee budget for the finance team costs more than this pipeline.

Now, I'm being a bit sneaky here. This is the steady-state cost for 4,000 invoices. During development and testing, we burned through about $200 in Textract and Bedrock calls because we kept reprocessing the same test set. And the real cost is engineering time — I spent about 3 weeks building this, plus another 2 weeks hardening it for production (that's Part 3). But the infrastructure cost? Embarrassingly cheap.

## Tradeoffs We Made Early

**Textract + Claude vs. Claude only:** We could skip Textract entirely and send PDFs directly to Claude's vision capability. We tested this. Claude's extraction was actually more accurate on messy invoices (87% vs Textract's 79%). But it cost 4x more per invoice and was 3x slower. For clean invoices (which are 70% of our volume), Textract was just as accurate. So we use Textract as the default and only escalate to Claude vision for the ones Textract flags as low-confidence.

**Step Functions vs. MWAA:** Already covered, but worth repeating: at our scale, Step Functions is 400x cheaper than MWAA ($0.50 vs $200/month). If we were orchestrating 20 different data pipelines, MWAA would make sense. For a single focused pipeline, it's overkill.

**DynamoDB vs. RDS PostgreSQL:** DynamoDB is schemaless, which means our invoice schema can evolve without migrations. When we added "purchase order number" as a field two months in, we just started writing it. No ALTER TABLE, no deployment. The tradeoff: DynamoDB's query patterns are limited. We can't do ad-hoc SQL joins. For our use case (lookup by ID, filter by vendor), that's fine. For analytics, we'd need to pipe data elsewhere.

**Synchronous vs. Asynchronous processing:** We process invoices asynchronously. An invoice uploaded at 9:03 AM might be fully processed by 9:05 AM. Not real-time, but our finance team doesn't need instant results — they batch-review once a day. If you need sub-second processing (fraud detection, real-time pricing), this architecture doesn't fit. Different problem, different design.

## What's Coming in Part 2

The architecture is the easy part. The hard part is making it actually work — handling the edge cases, writing the extraction prompts that don't hallucinate line items, validating that $1,247.50 on the invoice matches the sum of line items.

In Part 2, I'll share the actual Python code for every component. Not pseudocode, not "left as an exercise." Working code that processes a real invoice from PDF to structured data.

---

*This is Part 1 of 3. Part 2: "Show Me the Code" covers the working implementation. Part 3: "Making It Survive Monday Morning" covers production hardening, cost optimization, and business outcomes.*
