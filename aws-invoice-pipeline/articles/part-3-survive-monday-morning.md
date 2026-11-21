# Making It Survive Monday Morning

*Part 3 of 3: AI + Data Engineering on AWS*

---

## Problem Statement

We had a working pipeline. Code that extracted invoices, classified them, validated them, and stored structured data in DynamoDB. It passed tests. It handled our sample set of 50 invoices beautifully.

Then we deployed it. And it met reality.

This article is about everything that happened after deployment. The monitoring we wished we'd built earlier. The cost explosion we didn't see coming. The tradeoff that seemed smart in theory and cost us two weeks of rework. And the business outcomes — because at the end of the day, nobody cares about your architecture if it doesn't move a number.

## What You'll Get From This Article

- Production monitoring setup using CloudWatch (dashboards, alarms, the metrics that actually matter)
- Cost optimization that reduced our annual spend from $12K to $1.8K
- The 3 tradeoffs that defined the project — what worked, what didn't, what we'd do differently
- Business outcomes with real numbers (error rates, time saved, money recovered)
- An honest retrospective on what surprised us

---

## Week 1: The Monday Morning Disaster

We deployed on a Friday. Classic mistake.

Monday morning, the finance team's vendor portal got slammed with 340 invoices. Our pipeline triggered 340 Step Functions executions simultaneously. Textract handles concurrency fine. But our Bedrock calls hit throttling limits — Haiku's default throughput on Bedrock is 100 requests per minute. We were submitting 340 in about 90 seconds.

Step Functions retried automatically (thank god for those retry configs), but the retries backed up. Some invoices took 15 minutes to process instead of 30 seconds. The Slack channel filled with "is the system down?" messages.

The fix was embarrassingly simple. We added a `Map` state with `MaxConcurrency: 20` to the Step Functions definition. Instead of processing all invoices in parallel, we capped at 20 concurrent executions. Throughput dropped from "chaotic burst" to "steady 40 invoices per minute." The entire Monday backlog cleared in 9 minutes.

```json
{
  "ProcessBatch": {
    "Type": "Map",
    "MaxConcurrency": 20,
    "ItemsPath": "$.invoices",
    "Iterator": {
      "StartAt": "ExtractText",
      "States": { "...same states as before..." }
    }
  }
}
```

Lesson: always think about what happens when your pipeline runs at 10x normal load. Because it will.

## Monitoring That Actually Matters

After the Monday incident, I spent two days setting up monitoring. Not "we have CloudWatch" monitoring — monitoring that tells you something useful.

Here's what we track and why:

```python
import boto3
from datetime import datetime

cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")


def publish_pipeline_metrics(
    invoice_id: str,
    stage: str,
    duration_ms: float,
    confidence: float,
    status: str,
    cost_usd: float,
):
    """
    Publish custom metrics to CloudWatch after each pipeline stage.
    """
    timestamp = datetime.utcnow()

    cloudwatch.put_metric_data(
        Namespace="InvoicePipeline",
        MetricData=[
            # How long each stage takes
            {
                "MetricName": "StageDuration",
                "Dimensions": [{"Name": "Stage", "Value": stage}],
                "Value": duration_ms,
                "Unit": "Milliseconds",
                "Timestamp": timestamp,
            },
            # Confidence distribution — are we getting dumber?
            {
                "MetricName": "ExtractionConfidence",
                "Value": confidence,
                "Unit": "None",
                "Timestamp": timestamp,
            },
            # Cost per invoice — catches budget blowouts early
            {
                "MetricName": "CostPerInvoice",
                "Value": cost_usd,
                "Unit": "None",
                "Timestamp": timestamp,
            },
            # Pass/fail rate
            {
                "MetricName": "ValidationResult",
                "Dimensions": [{"Name": "Status", "Value": status}],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": timestamp,
            },
        ],
    )
```

Three metrics matter more than the rest:

**Confidence trend over time.** If average confidence drops from 0.87 to 0.79 over two weeks, something changed. Maybe a vendor started using a new invoice template. Maybe Bedrock updated the model. Either way, you want to know before the finance team tells you.

**Flagged-for-review rate.** We target 15% or less. If it creeps above 20%, the pipeline is creating more work than it's saving. We set a CloudWatch alarm on this:

```python
cloudwatch.put_metric_alarm(
    AlarmName="HighReviewRate",
    Namespace="InvoicePipeline",
    MetricName="ValidationResult",
    Dimensions=[{"Name": "Status", "Value": "flagged_for_review"}],
    Statistic="Sum",
    Period=86400,  # Daily
    EvaluationPeriods=1,
    Threshold=800,  # 20% of 4000
    ComparisonOperator="GreaterThanThreshold",
    AlarmActions=["arn:aws:sns:us-east-1:123456789:ops-alerts"],
)
```

**Cost per invoice.** Our budget assumes $0.006 per invoice. If a new vendor starts sending 20-page invoices, the Textract and token costs spike. We catch this with a per-invoice cost alarm at $0.02 (3x our average).

The CloudWatch dashboard looks like this:

```
┌──────────────────────────────────────────────────┐
│  Invoice Pipeline — Daily Dashboard               │
├──────────────────┬───────────────────────────────┤
│ Processed Today  │ Avg Confidence     │ Cost MTD │
│    187           │    0.86            │  $18.42  │
├──────────────────┴───────────────────────────────┤
│ [Chart: Confidence trend — last 30 days]          │
│  0.90 ─────────────────────                      │
│  0.85 ───────────────────────────────────────    │
│  0.80 ─                                          │
├──────────────────────────────────────────────────┤
│ [Chart: Status breakdown — last 7 days]           │
│  Approved:  82%  ████████████████░░░░           │
│  Flagged:   14%  ████░░░░░░░░░░░░░░░           │
│  Failed:     4%  █░░░░░░░░░░░░░░░░░░           │
├──────────────────────────────────────────────────┤
│ [Chart: Cost per invoice — last 30 days]          │
│  $0.008 ────────────────────────────             │
│  $0.006 ──────────────────────                   │
│  $0.004 ───                                      │
└──────────────────────────────────────────────────┘
```

## The Cost Optimization Story

Month 1 in production, our bill was $986. Annualized: ~$12K/year. The pipeline was clearly worth it ($238K in savings vs $12K in cost), but I knew we were wasting money. Here's where and how we fixed it.

**Problem 1: Processing non-invoices through the full pipeline.**

15% of incoming documents were delivery receipts, packing slips, or junk. They'd hit Textract ($1.50/1K pages), get classified by Haiku ("not an invoice"), and stop. We were spending ~$100/month on Textract for documents we immediately threw away.

**Fix:** Add a pre-filter Lambda that checks the file name and S3 metadata tags before triggering the pipeline. Vendors tag their uploads as "invoice" or "receipt" — we trust that tag for the easy cases and only run Textract on untagged or ambiguous documents.

```python
def pre_filter(event, context):
    """
    Quick filter before expensive processing.
    Returns True if document should enter the pipeline.
    """
    s3 = boto3.client("s3")
    key = event["detail"]["object"]["key"]
    bucket = event["detail"]["bucket"]["name"]

    # Check S3 metadata tags
    tags = s3.get_object_tagging(Bucket=bucket, Key=key)
    tag_dict = {t["Key"]: t["Value"] for t in tags["TagSet"]}

    doc_type = tag_dict.get("document_type", "unknown")

    if doc_type in ("receipt", "packing_slip", "delivery_note"):
        # Log it, skip it
        logger.info(f"Skipped non-invoice: {key} (tagged as {doc_type})")
        return {"process": False, "reason": f"Tagged as {doc_type}"}

    return {"process": True, "bucket": bucket, "key": key}
```

Savings: ~$100/month.

**Problem 2: Running Sonnet on every invoice.**

We were using Sonnet for all extraction because it was more accurate. But most of our invoices are straightforward — typed, single page, standard format. Haiku handles those fine.

**Fix:** Two-tier extraction. Textract first. If Textract confidence is above 0.90 and the document is single-page, use Haiku for extraction (cheap, fast). If confidence is below 0.90 or multi-page, escalate to Sonnet.

```python
def choose_extraction_model(textract_confidence: float, page_count: int) -> str:
    """Pick the right model based on document complexity."""
    if textract_confidence > 0.90 and page_count == 1:
        return "anthropic.claude-3-haiku-20240307-v1:0"
    else:
        return "anthropic.claude-3-5-sonnet-20241022-v2:0"
```

Result: 70% of invoices use Haiku, 30% use Sonnet. Bedrock cost dropped from $13.50/month to $5.80/month.

**Problem 3: Re-extracting unchanged invoices.**

Some vendors re-upload the same invoice (corrections, duplicates). We were processing them from scratch every time.

**Fix:** Hash the PDF content on upload. Check DynamoDB for the hash before processing. If it exists, skip.

```python
import hashlib
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb", region_name="us-east-1")


def compute_file_hash(bucket: str, key: str) -> str:
    """SHA-256 hash of file content for dedup."""
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    content = obj["Body"].read()
    return hashlib.sha256(content).hexdigest()


def is_duplicate(file_hash: str) -> bool:
    """Check if we've already processed this exact file."""
    table = dynamodb.Table("processed-invoices")
    response = table.query(
        IndexName="file-hash-index",
        KeyConditionExpression=Key("file_hash").eq(file_hash),
    )
    return response["Count"] > 0
```

Savings: ~$15/month (catches about 8% duplicate uploads).

**After optimization:**

| Item | Before | After |
|------|--------|-------|
| Textract | $6.00 | $4.20 (skip non-invoices) |
| Bedrock | $13.50 | $5.80 (Haiku for simple) |
| Lambda | $1.20 | $1.50 (added pre-filter) |
| Step Functions | $0.50 | $0.50 |
| DynamoDB | $3.00 | $3.50 (added hash index) |
| Other (S3, SNS, EB) | $0.16 | $0.16 |
| **Total** | **$24.36/month** | **$15.66/month** |

Wait — I said the initial cost was $986/month and we got it to $1.8K/year. Let me explain that gap, because it confused me too when I first looked at the numbers.

The $986 was month 1: development and production running simultaneously, reprocessing test sets dozens of times, no optimizations, Sonnet running on every invoice. The $24/month from Part 1 was the theoretical steady-state for just the pipeline services.

After optimization, the pipeline services cost about $16/month. But the real cost includes everything that keeps it running: CloudWatch dashboards ($3/each, we have 2), custom metrics ($0.30/metric/month, we track 12), log storage for 4,000 invoices of debug output (~$8/month), Athena queries from the finance team (~$2/month), and the nightly export Lambda. All that operational overhead adds about $35/month.

So the honest number: **~$50/month all-in, or $600/year.** I rounded up to $150/month in my early planning to include a buffer for spikes, testing, and the occasional reprocessing run. In practice, we've averaged $50-70/month over the last 6 months.

## The Three Tradeoffs

Every project has decisions that define it. Here are ours.

### Tradeoff 1: Textract + Claude vs. Claude Vision Only

**What we chose:** Hybrid. Textract for OCR, Claude for intelligence.

**What we considered:** Sending PDFs directly to Claude's vision capability. It's simpler — one API call instead of two. And Claude vision actually scored higher on messy, handwritten invoices (87% accuracy vs Textract's 79%).

**Why we chose hybrid:** Cost. Claude vision on Sonnet costs roughly $0.01 per page (image tokens are expensive). Textract costs $0.0015 per page. At 4,000 pages/month, that's $40 vs $6. For the 70% of invoices that are clean and typed, Textract's accuracy is identical to Claude's. We only lose on the 30% that are messy — and for those, we escalate to Sonnet anyway.

**Would I do it again?** Yes. The hybrid approach is more complex to build but saves ~$400/year. If our volume was under 500 invoices/month, I'd go Claude vision only and keep it simple.

### Tradeoff 2: DynamoDB vs. RDS PostgreSQL

**What we chose:** DynamoDB.

**What we considered:** RDS PostgreSQL, because SQL is comfortable and the finance team wanted to query extracted data directly.

**Why we chose DynamoDB:** Zero operational overhead. No connection pooling, no vacuuming, no storage provisioning. At our scale, on-demand DynamoDB costs $3/month. The smallest RDS instance costs $15/month, and that's before you add Multi-AZ for reliability ($30/month).

**The tradeoff that bit us:** The finance team couldn't query DynamoDB with SQL. They wanted to run things like "show me all invoices from Acme Corp over $5,000 in Q3." DynamoDB can't do that without a GSI for every query pattern.

**How we fixed it:** We added a nightly Lambda that exports DynamoDB to S3 as Parquet, then the finance team queries it through Athena. Total cost for Athena: about $2/month (they scan maybe 500MB of data). It's a workaround, but it works.

**Would I do it again?** Honestly, no. If I started over, I'd use RDS PostgreSQL. The $12/month premium is worth it for the query flexibility. DynamoDB was the right choice for the pipeline's write-heavy pattern, but I underestimated the read patterns the business needed. This cost us about two weeks of rework building the Athena export.

```python
# The nightly export Lambda we had to build
# (This is the rework I mentioned)
def export_to_s3(event, context):
    """Export DynamoDB table to S3 as Parquet for Athena."""
    import pandas as pd

    table = dynamodb.Table("processed-invoices")

    # Full scan — fine for 4K records, terrible at scale
    items = []
    response = table.scan()
    items.extend(response["Items"])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            ExclusiveStartKey=response["LastEvaluatedKey"]
        )
        items.extend(response["Items"])

    df = pd.DataFrame(items)

    # Convert Decimal to float for Parquet
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].apply(
            lambda x: float(x) if isinstance(x, Decimal) else x
        )

    # Write to S3 as Parquet
    # Note: requires s3fs and pyarrow — pip install s3fs pyarrow
    s3_path = f"s3://invoices-analytics/{datetime.now().strftime('%Y/%m/%d')}/invoices.parquet"
    df.to_parquet(s3_path, index=False)

    return {"exported": len(items), "path": s3_path}
```

### Tradeoff 3: One Prompt vs. Multi-Stage Prompting

**What we chose:** Multi-stage. Classification with Haiku, then extraction with Sonnet.

**What we considered:** A single prompt that does everything — classify AND extract in one call.

**Why multi-stage won:** Accuracy. When we combined classification and extraction into one prompt, Claude would sometimes "hallucinate" extraction fields for non-invoice documents. It would see a delivery receipt and still try to extract an invoice number, making one up from the tracking number. Splitting the stages meant classification acts as a gate — if it's not an invoice, we never ask for extraction at all.

**Would I do it again?** Absolutely. Multi-stage prompting is slightly more expensive (two API calls instead of one) but dramatically more reliable. The "gate" pattern — classify first, then extract only if relevant — is one of the most useful patterns I've found in AI pipeline work.

## Business Outcomes

Eight months in, here are the actual numbers:

**Error rate:** Down from 8% to 1.7%. The remaining 1.7% are edge cases — handwritten invoices in languages our prompts don't handle, invoices with creative formatting that confuse even Claude. The finance team handles these in the human review queue.

**Processing time:** Down from 3 business days (manual) to 9 minutes (automated) for the 85% that pass automatically. Flagged invoices get reviewed within 24 hours.

**Headcount impact:** The team went from 3 FTEs to 1 FTE on invoice processing. The other two people moved to vendor management and payment optimization — higher-value work they didn't have time for before.

**Financial impact:**
- Labor savings: $110K/year
- Error reduction (fewer duplicate payments, late fees): ~$145K/year
- Pipeline cost: ~$600-840/year (averages $50-70/month all-in)
- **Net annual savings: ~$254K/year**

**Invoices processed:** 31,847 in 8 months. Auto-approved: 85.2%. Flagged for review: 12.1%. Failed (dead-lettered): 2.7%.

The CFO sent an email to the engineering team. Subject line: "The invoice project actually worked." That was worth more than the cost savings.

## What I'd Do Differently

**Start with RDS, not DynamoDB.** I already covered this. The query flexibility would have saved two weeks.

**Build monitoring from day one, not after the Monday disaster.** The monitoring setup took two days. If I'd built it before deployment, we'd have caught the concurrency issue in staging.

**Test with a full month's volume, not 50 samples.** Our test set was clean, well-formatted invoices. Reality included scanned faxes (yes, in 2024), invoices in German, and one vendor who sends invoices as PowerPoint files. I'm not joking.

**Set up the Athena export from the start.** The finance team was always going to want SQL access. Building it reactively instead of proactively created unnecessary friction.

## When This Architecture Doesn't Fit

I want to be direct about limitations:

**High volume (100K+ invoices/month):** Step Functions gets expensive at scale — you'd want Airflow or a custom orchestrator. Textract has throughput limits that require batching strategies. The whole architecture shifts from "serverless event-driven" to "managed batch processing."

**Real-time requirements:** If you need extracted data in under 5 seconds (fraud detection, real-time pricing), this async pipeline doesn't work. You'd need a streaming architecture with pre-warmed Lambda or ECS tasks.

**Multi-language at scale:** Our prompts are English-optimized. They handle Spanish and French reasonably well. German and Japanese need dedicated prompts. If you're processing invoices in 15 languages, you need a prompt-per-language strategy with automatic language detection.

**Regulated industries with audit requirements:** Our pipeline logs what it extracted, but it doesn't provide explainability for WHY it extracted a specific value. If auditors need to understand the decision chain (healthcare, banking), you need to add explainability layers — which adds cost and complexity.

## Wrapping Up

Three articles. One pipeline. Here's what we built:

An AI-powered invoice processing system on AWS that takes a PDF from S3, extracts text with Textract, classifies and extracts structured data with Claude on Bedrock, validates the output, stores it in DynamoDB, and alerts humans when something looks off.

It costs about $50-70/month. It processes 4,000 invoices. It saves ~$254K/year. It took 5 weeks to build.

The technology isn't the hard part. Textract, Bedrock, Step Functions — they all work as advertised. The hard part is the boring stuff: handling edge cases, monitoring for silent drift, making cost-driven decisions about which model to use, and building the feedback loop that turns flagged invoices into training data for better prompts.

If you're a data engineer thinking about adding AI to your pipelines, start small. Pick one messy manual process. Build the simplest version that works. Deploy it, watch it break, fix it. The architecture patterns in this series — Textract for the mechanical, Claude for the intelligent, Pydantic for the contract, Step Functions for the glue — apply to way more than invoices. Document processing, email classification, support ticket routing, compliance checking. The shape is the same. The details change.

Good luck. And deploy on a Monday, not a Friday.

---

*This is Part 3 of 3. Start from the beginning: Part 1 — "We Were Drowning in Invoices" covers the problem and system design. Part 2 — "Show Me the Code" has the complete working implementation.*
