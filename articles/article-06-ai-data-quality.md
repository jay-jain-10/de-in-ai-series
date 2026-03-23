# The Semantic Data Quality Layer Your Warehouse Is Missing

*Part 6 of the "Data Engineering in the Age of AI" Series*

---

## Problem Statement

Great Expectations catches format errors and null checks — the easy 60%. But your healthcare pipeline just loaded 3% of records where diagnosis codes don't match procedure codes, medication dosages are physiologically impossible, and temporal sequences are logically backwards. Syntactic validation passed. Semantic validation doesn't exist. Your warehouse has confident-looking garbage.

## What You'll Get From This Article

This article walks through a **two-layer validation architecture (syntactic rules + semantic AI sampling) with human feedback loop** for healthcare data. You'll get:

- Two-layer validation: syntactic 100% rule-based → semantic AI-sampled with human feedback
- Risk-based sampling strategy (100% for safety-critical, 20% for medium, 3% for low)
- Precision/recall calibration protocol (monthly 200-sample review, Cohen's kappa >0.7)
- Quarantine table as audit log tracking validation decisions
- Cost breakdown: ~$1,700-2,000/month on AWS for 10M daily records
- DE fundamentals: data contracts for quality layers, quality as dimension table, schema evolution for rules

---

I watched a healthcare data team debug a data quality issue that consumed three weeks of engineering time. A patient's diagnosis code didn't match their procedure code in approximately three percent of records. To every deterministic validator in the pipeline—Great Expectations, Soda, dbt schema tests, custom SQL assertions—this data looked fine. Both codes were valid ICD-10 codes. Both columns were populated with data. No nulls. No out-of-range values. No schema type mismatches. The data passed one hundred percent of syntactic checks.

But semantically it was broken. You don't perform a total knee replacement on a patient whose primary diagnosis is acute sinusitis. The codes are individually valid. Together they're nonsensical. The data failed the comprehension test. It was syntactically valid but semantically incoherent.

This is where rule-based data quality hits its architectural ceiling. At the sixty-percent-caught threshold, you've captured the easy failures. Nulls. Type violations. Format errors. Range violations. The remaining forty percent of real-world data issues require something different. They require semantic reasoning. They require understanding that a diagnosis code is logically incompatible with a procedure code. They require reading the data the way a domain expert would read it.

This article walks you through the two-layer architecture that bridges this gap: deterministic rule-based validation for breadth, AI semantic validation for depth, and the organizational patterns that make this work at scale. The layered approach is the only one that survives production.

## The Two Distinct Layers of Data Quality

Most data quality frameworks blur a critical distinction. Understanding this distinction determines whether your validation layer plateaus at sixty percent or climbs above ninety-five percent.

```
┌─────────────────────────────────────────────────────────────────────────┐
│              TWO-LAYER DATA QUALITY ARCHITECTURE                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────┐                                                           │
│  │ Raw Data │     LAYER 1: SYNTACTIC (100% of rows, milliseconds)      │
│  │ (10M     │───▶ ┌────────────────────────────────────────────────┐   │
│  │  rows)   │     │  Great Expectations / dbt Tests                │   │
│  └──────────┘     │  • NOT NULL checks    • Type validation        │   │
│                    │  • Range validation   • Referential integrity  │   │
│                    │  • Uniqueness         • Format/regex checks    │   │
│                    └──────────┬──────────────────┬─────────────────┘   │
│                               │ PASS             │ FAIL                │
│                               ▼                  ▼                     │
│                    ┌──────────────────┐  ┌──────────────────┐         │
│                    │ Passed Records   │  │  Quarantine      │         │
│                    │ (9.5M rows)      │  │  (500K rows)     │         │
│                    └────────┬─────────┘  │  reason: L1_fail │         │
│                             │            └──────────────────┘         │
│                             ▼                                         │
│      LAYER 2: SEMANTIC (stratified sample, seconds per record)        │
│      ┌────────────────────────────────────────────────────────┐       │
│      │  Risk-Based Sampling:                                   │       │
│      │  • High-risk (healthcare): 100% → 1M records           │       │
│      │  • Medium-risk (finance):   20% → 800K records          │       │
│      │  • Low-risk (marketing):     3% → 150K records          │       │
│      │                                                         │       │
│      │  Claude Sonnet + Few-Shot Domain Examples               │       │
│      │  Output: {is_consistent, anomaly_type, confidence}      │       │
│      └──────────┬─────────────────────────┬────────────────────┘       │
│                 │ CONSISTENT               │ ANOMALY DETECTED          │
│                 ▼                          ▼                           │
│      ┌──────────────────┐      ┌──────────────────┐                  │
│      │  Production      │      │  Quarantine       │                  │
│      │  Warehouse       │      │  (flagged records) │                  │
│      │  (Snowflake/BQ)  │      │  reason: L2_fail  │                  │
│      └──────────────────┘      │  + anomaly_type   │                  │
│                                │  + confidence     │                  │
│                                └────────┬──────────┘                  │
│                                         ▼                              │
│                                ┌──────────────────┐                   │
│                                │  Human Review     │                   │
│                                │  • Confirm error  │                   │
│                                │  • False positive  │                   │
│                                │  • Fix upstream    │                   │
│                                │  → Feedback loop  │                   │
│                                └──────────────────┘                   │
└─────────────────────────────────────────────────────────────────────────┘
```

**Syntactic data quality** answers a precise question: does this data conform to the schema? Is the column the right data type? Are required fields populated? Do values fall within declared ranges? Is the format valid? Does the email string match email regex? Does the date parse as a date? Great Expectations, dbt tests, and Soda excel at this. They're fast (scanning 10 million rows in minutes). They're cheap (milliseconds per record). They catch obvious problems deterministically.

The layer one validator pattern: parses a claims table, checks that claim_id is not null, checks that claim_amount is numeric and between 0 and 1,000,000, checks that claim_date is a valid date and not in the future, checks that patient_id exists in the patients table (referential integrity), checks that diagnosis_code matches the ICD-10 format. All syntactic. All deterministic. All provable.

A healthcare data warehouse with two hundred disparate sources can run comprehensive syntactic checks on every row in minutes. Null checks. Type checks. Range checks. Pattern checks. Referential integrity. Uniqueness constraints.

**Semantic data quality** answers a different question: does this data make sense given domain knowledge and business logic? If a diagnosis code is "acute sinusitis," should the procedure code be "total knee replacement"? If a doctor's clinical notes say "patient reports severe pain," should the pain severity field say "1 (none)"? If a customer's typical transaction range is fifty to two hundred dollars, is a five-thousand-dollar transaction an anomaly or is there context that explains it? If a supply chain shipment leaves Shanghai and arrives in Los Angeles, is a delivery date of the same day possible given that flight takes twelve hours, plus clearance, plus ground transport?

Semantic validation requires reasoning. It requires understanding domain knowledge, business logic, and field relationships. It requires context. This is where language models excel. They excel at comprehension. They understand contradiction. They assess likelihood.

**The critical insight:** most teams conflate syntax with semantics. They claim, "Our data quality is excellent because it passes all validation checks." That's like claiming software is correct because it passes unit tests. It conforms to specs, but does it actually work? Most production data carries silent semantic errors. The data looks right. It passes all rules. In context, it's wrong.

The examples:

- **Address changing weekly:** Syntactically valid. Address field is populated, correct data type, not null. Semantically suspicious. Most humans don't move weekly. Red flag for data quality issues.

- **Patient lab result below normal range:** Syntactically valid (numeric, within database column range). Semantically expected if the patient takes medication that suppresses that marker. Contextually correct.

- **Diagnosis doesn't match procedure:** Syntactically valid (both are valid codes). Semantically incoherent.

- **Invoice amount is zero:** Syntactically valid (numeric, not null). Semantically suspicious. Why is there an invoice with zero amount?

The rule-based validator catches zero. The semantic validator catches the pattern.

To build semantic validators, you need to:

1. Understand what contradictions look like in your domain
2. Provide examples to the model
3. Have the model reason about the specific record
4. Receive structured output indicating semantic issues
5. Measure precision and recall carefully

This is where Claude Sonnet or Opus are typically deployed, reading the record and its context.

## Why Rule-Based Tools Are Necessary But Hit Hard Limits

Let me be explicit: I'm not suggesting you replace Great Expectations or dbt tests. I'm suggesting they're not sufficient. The analogy is unit tests vs integration tests. Different purposes. You need both.

Rule-based tools have profound strengths:
- **Speed:** Milliseconds per row when checking nulls, ranges, formats
- **Cost:** Pennies per million rows (Great Expectations runs on your data warehouse)
- **Determinism:** Same input always produces same output
- **Auditability:** You can prove exactly why a record passed or failed

But they have hard limits. They can't reason about context. They can't understand domain knowledge. They can't make probabilistic judgments about "probably wrong." They see patterns, not meaning.

Here's the practical breakdown of what rule-based validators catch:

- Null checks: catches 100% of missing required values
- Type validation: catches 100% of schema mismatches
- Format validation: catches 100% of regex/pattern violations
- Range validation: catches 100% of out-of-bounds values
- Referential integrity: catches 100% of orphaned foreign keys
- Uniqueness: catches duplicate primary keys

And here's what they miss:

- Contradiction between fields (diagnosis vs procedure)
- Inconsistency between structured fields and unstructured text (pain_level vs clinical_notes)
- Implausible patterns (address changing daily, but no moving company involved)
- Contextual anomalies (zero-dollar invoice, but there was an invoice created)
- Industry/domain-specific rules (this drug isn't prescribed for this diagnosis)

This is where semantic validation comes in. The layer two validator receives a stratified sample of rows that passed layer one. Each row is sent to Claude Sonnet with few-shot examples of real semantic errors from your domain. The model reads the entire record—all fields, all relationships—and generates structured JSON: `{is_consistent: boolean, anomaly_type: string, confidence: 0-1, reasoning: string}`. The response indicates whether the record makes sense.

This two-layer approach works because:
1. Layer one is cheap, fast, comprehensive (100% of rows)
2. Layer one eliminates obvious garbage, reduces signal-to-noise
3. Layer two is expensive, slow, used selectively (5-20% of rows)
4. Layer two catches what rules miss
5. Together they achieve >90% error detection at reasonable cost

## The Sampling Strategy: Risk-Based, Not Random

Here's where most teams fail. They can't afford to validate 10 million rows with Claude Sonnet. At $0.003 per 1K tokens, and assuming 300-500 tokens per record, that's $30-50K per month for a single validation. That's unsustainable.

The solution is intelligent stratified sampling. You don't validate randomly. You validate strategically.

Stratification example:

- **Healthcare patient records (safety-critical):** 100% sample. One semantic error could harm a patient. Cost: high. Risk of missing errors: unacceptable.

- **Insurance claims (financial-critical):** 100% sample. Fraud is expensive. Cost: high. Risk: unacceptable.

- **Customer addresses (marketing data):** 5% stratified sample (all ICU/emergency records if healthcare, all high-value customers if finance, all recent changes). Cost: low. Risk: acceptable because most address errors don't cause critical problems.

- **Product inventory (reference data):** 1% random sample. Cost: minimal. Risk: low because errors are usually caught downstream.

To implement this, you need infrastructure that measures error rates per source:

1. For each source system, track historical error rate (how many errors per 1000 records when validated by humans)
2. Score each source 0-100 on risk (multiplier of error rate × business criticality × consequences)
3. Route 100% of high-risk sources (score >80) to semantic validation
4. Route 20% of medium-risk (50-80) to semantic validation
5. Route 2-5% of low-risk (<50) to semantic validation
6. Monitor for source degradation: if a low-risk source's error rate spikes, increase sampling

Implementation: dbt models that compute source_risk_score, a sampling model that routes records based on risk, and a monitoring query that alerts when sampling rates should change.

Example math: 10 million records, distributed as 1M high-risk (100%), 4M medium-risk (20%), 5M low-risk (3%).

- High-risk: 1M records × $0.003 per record = $3,000
- Medium-risk: 4M × 0.20 × $0.003 = $2,400
- Low-risk: 5M × 0.03 × $0.003 = $450
- Total monthly semantic validation: ~$6,000 for 10M records, or $0.0006 per record average

Compare to: validating all 10M with AI = $30,000. Stratified sampling reduces cost by 80% while catching 95% of errors.

## Calibration and Trust

Here's the problem nobody talks about: when the AI validator says a record is wrong, how confident should you be?

If your semantic validator has 85% precision, one in six alerts is a false positive. That's acceptable for lower-criticality data. If it has 50% precision, half the alerts are wrong. That's unacceptable.

You need to measure precision and recall separately for different domains. Your validator might have:
- 92% precision on healthcare data (rare false positives, important)
- 65% precision on supply chain data (many false positives, less critical)
- 78% precision on financial data (some false positives, medium criticality)

You measure precision by having humans review a random sample of AI flags. Of 100 flagged records, did humans agree with the flag on 85? That's 85% precision.

To set thresholds correctly:
- Healthcare, safety-critical: alert at 60% confidence (accept false positives, catch errors)
- Financial, fraud-related: alert at 70% confidence
- Marketing, informational: alert at 80% confidence (need high precision to avoid alert fatigue)

The validator includes confidence scores in its output. You use the confidence threshold to adjust alert routing. This requires monthly calibration—have humans review samples and measure precision. If precision drops below threshold, adjust your prompt, adjust your examples, or lower your confidence threshold.

**Calibration Protocol**: Monthly, pull 200 random records flagged by the semantic validator. Have two independent reviewers assess each. Compute: (1) Precision = records where both reviewers agree with AI flag / total flagged. (2) Inter-rater agreement (Cohen's kappa) between the two reviewers. If kappa < 0.7, your review process needs standardization before you can trust precision numbers. Track precision monthly. If it drops below 80% for any domain, adjust: add more few-shot examples, refine the prompt, or lower the confidence threshold. This calibration loop is what turns a validation experiment into a production system.

## Two-Layer Architecture in Practice

Layer 1 (deterministic) runs in minutes on 100% of rows:

```
For each record in table:
  Check required fields are not null
  Check numeric fields are numeric and in range
  Check dates are valid and not future
  Check foreign keys exist
  Check no duplicates on unique columns
```

This runs in SQL or Great Expectations. 10 million rows in 2-3 minutes on a modest data warehouse. Cost: minimal (warehouse compute, maybe $5 per run).

Records that fail layer 1 go to quarantine table. They don't proceed downstream.

Layer 2 (semantic) samples records that passed layer 1:

```
For each record in sampled_records:
  Assemble context: all fields from the record
  Add few-shot examples: 3-5 real examples of semantic errors from your domain
  Send to Claude Sonnet with prompt: "Is this record logically consistent given industry standards?"
  Receive structured JSON response
  If confidence > threshold AND anomaly detected:
    Send to quarantine table with anomaly_type and confidence
```

Cost: $6K/month for 10M records with stratified sampling.

Records flagged by layer 2 also go to quarantine.

Remaining records proceed to production warehouse.

## The Quarantine Table: The Core Asset

The quarantine table is not a trash bin. It's your most valuable quality asset.

Every record that fails layer 1 or layer 2 goes here with metadata: which check failed, layer 1 or layer 2, anomaly_type, confidence, timestamp. This table is reviewed. Some records need upstream fixes (source system is broken). Some are validation false positives (the validator misunderstood context). Some are real errors.

Human reviewers mark each quarantined record: `decision = {fix_upstream, accept_as_is, confirm_error}`. This feedback improves the system.

If humans frequently override a rule, that rule is probably wrong. If humans frequently disagree with the semantic validator, you need better examples or prompt adjustment. The quarantine table is your signal for system improvement.

Cost: 1-2 FTEs reviewing quarantined records daily. For 10M records with 3-5% error rate, you're quarantining 300-500K records. If each takes 10 seconds to review, that's 800-1400 human-hours per month. You can't review everything. Instead, sample 10% of quarantined records for review. That's 80-140 hours per month = 1 FTE at partial time.

## When AI Quality Checks Are Overkill

Not everything needs semantic validation. Simple data with well-defined rules doesn't benefit from AI. Integers between 0-100? Rule-based check is perfect. Prices that never go negative? Rule-based works. Recent timestamps? Rules are enough.

Semantic validation is only cost-justified when:
1. The cost of a silent error downstream is high (healthcare, finance)
2. Errors are subtle and context-dependent (diagnosis vs procedure, text contradicts structure)
3. The domain has domain-specific knowledge that a rule-based system can't encode (what diagnosis codes pair with what procedures)
4. You have rich data with multiple fields that can be cross-validated

For simple reference data or highly structured data with no context, rule-based validation is sufficient and more cost-effective.

## Human-in-the-Loop at Production Scale

This is where most teams miss the real value. Quarantined records are reviewed by humans. But the review process has to be designed carefully, or it becomes a bottleneck and gets abandoned.

The workflow: a human reviewer receives a quarantined record. The UI shows: original data (all fields), the specific validation failure, historical context (previous versions of this record, similar records), and a decision dropdown. The reviewer's job is not to be an expert. It's to be a triage system: is this a real error, a false positive, or something upstream that needs fixing?

Typical decisions per reviewer: 15-20 records per hour. For 500K quarantined records per month, that's 25K-30K reviews. You need 1-2 FTEs for high-throughput systems. The cost: $80-120K per year.

The human reviewers feed back into the system. Every decision is logged. At the end of the month, you compute precision of the semantic validator: of 1000 records the validator flagged, humans agreed with 850. That's 85% precision. You adjust your confidence threshold accordingly.

Over time, the review data informs the system:

- If humans frequently override a syntactic rule, that rule is probably too strict
- If humans frequently disagree with semantic flags, you need better prompt examples
- If humans mark a particular source as consistently problematic, increase its sampling rate
- If certain anomaly_types are frequently false positives, adjust the semantic validator's logic

The human loop is where the system learns and improves.

For healthcare, this human loop is non-negotiable. Patient safety depends on it. For marketing data, you might skip it and just route quarantined records to a review table that analysts look at occasionally. The rigor should match the criticality of the data.

## Data Engineering Fundamentals: Quality as Infrastructure

As your data quality layers mature, the systems thinking required shifts. Quality isn't just a validation process anymore. It's infrastructure. It requires the same rigor that you apply to pipelines, schemas, and compute.

**Idempotency:**

Both Layer 1 and Layer 2 are idempotent. Running them twice on the same dataset produces the same quarantine results. This matters because Airflow retries are common. A failed Layer 2 run can be safely re-executed without double-quarantining records. Build this explicitly: before flagging a record in the quarantine table, check if it's already there with the same check_id. If so, skip it. This prevents a re-run from duplicating work or creating duplicate quarantine entries. The idempotency guarantee means your quality layer integrates cleanly with retry-heavy orchestration systems.

**Data Contracts for Quality:**

The quality layers themselves have contracts. Layer 1 guarantees: "all records passing this layer have non-null required fields, valid types, and valid ranges." Layer 2 guarantees: "sampled records passing this layer have been checked for semantic consistency with X% confidence." Downstream consumers can rely on these guarantees. Publish these contracts like any other data contract. Consumers should know: if they depend on data that passed quality validation, what exactly have they been guaranteed? If a record passed Layer 1, they can assume certain structural properties. If it passed Layer 2, they have semantic assurance to a specific confidence level. This clarity prevents downstream surprises.

**SLAs:**

Quality checks are not instant. They have time requirements and they fail. Layer 1 SLA: complete within 10 minutes for 10M rows. Layer 2 SLA: complete within 2 hours for sampled records. Human review SLA: quarantined records reviewed within 48 hours. Alert if any SLA is breached. If Layer 1 exceeds 10 minutes, investigate: is the warehouse overloaded? Are the checks inefficient? Are you checking too many records? If human review backlog exceeds 48 hours, escalate. This is the operational discipline that separates hobby quality systems from production ones.

**Lineage:**

Every quarantined record carries metadata: which layer flagged it (L1 or L2), which specific check failed, the confidence score (for L2), the timestamp, and the reviewer's decision (if reviewed). This lineage enables: root cause analysis (which check is catching the most errors?), trend detection (is source X degrading over time?), and precision measurement (what percentage of Layer 2 flags do humans agree with?). Track this in your quarantine table as:

```
quarantine_record {
  record_id: uuid,
  source_system: string,
  flagged_by_layer: enum(L1, L2),
  check_name: string,
  anomaly_type: string (if L2),
  confidence: float (if L2),
  flagged_timestamp: timestamp,
  reviewer_decision: enum(fix_upstream, accept_as_is, confirm_error, null),
  reviewed_timestamp: timestamp (null if not reviewed),
  reviewed_by: user_id (null if not reviewed),
  notes: string
}
```

This table is not a trash bin. It's an audit log that tracks data quality over time.

**Schema Evolution for Quality Rules:**

As your data evolves, quality rules must evolve too. Adding a new source system means adding new Layer 1 rules and new Layer 2 few-shot examples. The quality framework must support versioned rule sets that are deployed like code — through PR review and CI/CD. When you add a new source, don't hand-code rules into a Lambda function. Create a rule configuration file (YAML or JSON) that defines Layer 1 checks:

```yaml
source: "salesforce_claims"
layer_1_rules:
  - rule_id: "claim_id_not_null"
    check: "NOT NULL"
    column: "claim_id"
    severity: "critical"
  - rule_id: "claim_amount_range"
    check: "BETWEEN 0 AND 1000000"
    column: "claim_amount"
    severity: "high"

layer_2_examples:
  - description: "diagnosis doesn't match procedure"
    example: {diagnosis: "sinusitis", procedure: "knee_replacement"}
    is_consistent: false
  - description: "valid diagnosis-procedure pair"
    example: {diagnosis: "knee_osteoarthritis", procedure: "knee_replacement"}
    is_consistent: true
```

Deploy this configuration through version control. When the config changes, CI/CD tests it against historical data to catch regression. This treats data quality rules as software artifacts, not ad-hoc checks.

**Quality as a Dimension Table:**

The quarantine table and review decisions form a quality dimension that can be joined back to the fact tables. This enables queries like: "What percentage of claims from source X required human review last month?" or "Which diagnosis codes generate the most semantic anomalies?" This quality metadata is as valuable as the data itself. Join it into your analytics warehouse:

```sql
SELECT
  source_system,
  COUNT(*) as total_records,
  SUM(CASE WHEN flagged_by_layer IS NOT NULL THEN 1 ELSE 0 END) as flagged_count,
  SUM(CASE WHEN flagged_by_layer = 'L1' THEN 1 ELSE 0 END) as l1_failures,
  SUM(CASE WHEN flagged_by_layer = 'L2' THEN 1 ELSE 0 END) as l2_failures,
  SUM(CASE WHEN reviewer_decision = 'confirm_error' THEN 1 ELSE 0 END) as confirmed_errors,
  ROUND(100.0 * SUM(CASE WHEN reviewer_decision = 'confirm_error' THEN 1 ELSE 0 END)
    / NULLIF(SUM(CASE WHEN flagged_by_layer IS NOT NULL THEN 1 ELSE 0 END), 0), 2) as error_rate_pct
FROM fact_table
LEFT JOIN quality_dimension ON fact_table.record_id = quality_dimension.record_id
GROUP BY source_system
ORDER BY error_rate_pct DESC;
```

This tells you: which sources are degrading, which checks are most useful, what your true error rates are. Quality data informs everything: source onboarding decisions, SLA setting, and where to invest next.

## Cloud Architecture: AWS vs GCP

**AWS Implementation (Healthcare data quality pipeline for 10M daily records):**

- **MWAA (Managed Workflows for Apache Airflow):** Orchestrates the quality pipeline. Airflow environment = ~$700/month. Schedules layer 1 daily, layer 2 sampling, review workflows. DAGs are straightforward: ingest → layer1 → layer2 sample → send to Claude → persist results.

- **Lambda (Layer 1 validator):** Great Expectations runs in parallel across worker Lambda functions. Concurrency 1000 = scaling Lambda with $0.20 per 1M invocations. Typical: 100-200 invocations per run. Cost: $5-10/month. Each Lambda runs GE checks on a partition of records.

- **ECS Fargate (Layer 2 semantic validator):** Because Fargate can run longer (up to 1 hour timeout), it's better for semantic validation. The service batches sampled records, sends them to Claude Sonnet, persists responses. 2-3 tasks per day × 2 vCPU/2GB RAM × 10 minutes = ~$1-2 per run. Cost: $30-40/month.

- **Snowflake:** Data warehouse. Standard edition with 1-2 credit compute cluster (auto-suspend). Costs ~$400/month. Storage for raw + quality metadata: 500GB = $100/month. Snowflake is the source of truth for all data being validated.

- **S3 (cache):** Stores recent samples for reference. Minimal cost (~$1/month).

- **RDS PostgreSQL (quarantine metadata, review tracking):** db.t4g.small = $40/month. Stores quarantine records, human reviews, audit trail.

- **Anthropic API (Layer 2 semantic calls):** 10M records × 5% average sampling rate = 500K records validated monthly. 300 tokens per record × $0.003 per 1K tokens = $0.50K per run. Cost: ~$500/month.

- **QuickSight (quality dashboard):** ~$150/month for one dashboard showing layer 1 pass rate, layer 2 anomaly type distribution, human review backlog, false positive rate over time.

- **Total AWS:** $1,700-2,000/month for infrastructure + API.

**Trade-offs within AWS:**
- **Lambda vs Fargate for Layer 1:** Lambda is cheaper but has timeout limits (15 minutes). If layer 1 validation takes longer than 15 minutes, you're forced to partition heavily. Fargate is more expensive but more flexible. For 10M records, you probably need Fargate or batch processing.
- **Batch + GPU compute as alternative:** AWS Batch with GPU-accelerated instances for very large datasets. More complex but can amortize compute across many jobs.
- **SNowflake vs Redshift:** Snowflake is easier for analysts to work with, better for exploratory quality analysis. Redshift is cheaper for pure ETL. For data quality, Snowflake's sharing and governance features are worth the cost.

**GCP Implementation:**

- **Cloud Composer:** Managed Airflow. Standard environment = ~$400/month. Same orchestration as MWAA.

- **Cloud Functions (Layer 1):** Parallel execution of GE checks. Billing: $0.0000002 per invocation + compute. Cost: ~$5-10/month for 200 invocations, similar to Lambda.

- **Cloud Run (Layer 2):** Services can run up to 1 hour. Scales to zero. Pricing: $0.00004 per vCPU-second + $0.0000025 per GB-second. A 2-vCPU, 2GB instance running 10 minutes = 1200 vCPU-seconds = $0.048. Two runs per day = $0.096 per day = ~$3/month. Much cheaper than Fargate.

- **BigQuery:** The data warehouse. Pricing: $1.25 per GB for on-demand queries. Typical monthly queries for QA: 500GB scanned = $625/month. Storage: 500GB = $2.50/month. Much cheaper than Snowflake for pure QA use cases, but less good for governance.

- **GCS (cache):** Minimal cost (~$0.50/month).

- **Cloud SQL PostgreSQL:** Smaller instances than AWS. db-g1-small = ~$50/month. Same quarantine metadata tracking.

- **Anthropic API:** Same $500/month for layer 2.

- **Looker (dashboard):** Included with BigQuery. No additional cost.

- **Total GCP:** $1,400-1,600/month.

**Trade-offs between cloud:**
- **Snowflake vs BigQuery:** Snowflake for teams that want a dedicated warehouse with data sharing and governance features. BigQuery for teams that want simpler SQL querying and lower cost. BigQuery lacks some enterprise governance features.
- **Cloud Run vs Cloud Functions:** Cloud Run is better for longer-running quality checks. Cloud Functions for short, simple tasks.
- **MWAA vs Cloud Composer:** Nearly identical. Pick the cloud you're already invested in.

## Building the Semantic Validator

The layer 2 validator is a microservice or Airflow task that:

1. Receives a batch of 10-100 sampled records
2. For each record, assembles context (all fields + historical versions)
3. Constructs a prompt: "You are a data quality expert in healthcare. Here are examples of semantically inconsistent records. Does this record have semantic issues? Why or why not?"
4. Sends the record + few-shot examples to Claude Sonnet
5. Receives structured JSON: `{is_consistent, anomaly_type, confidence, reasoning}`
6. Persists results to PostgreSQL (quarantine metadata) + Snowflake/BigQuery (for analysis)
7. Routes records with is_consistent=false to quarantine table

The prompt engineering is critical. Good examples make the difference between 70% and 95% precision. You need 3-5 real examples from your domain where the record is definitely wrong. For healthcare: a diagnosis-procedure mismatch. A lab value that contradicts the clinical note. A medication that's contraindicated for the diagnosis.

The cost per record is approximately $0.003 (3 tokens input + 50 tokens output × Sonnet pricing). For 500K semantic validations per month, that's $1,500/month. You can optimize with model tiering: route records that are obviously consistent (high rules confidence) to Haiku for cheaper validation ($0.0008 per record = $400/month for Haiku vs $1500 for Sonnet). You might route 60% to Haiku and 40% to Sonnet, blending costs.

## Skills You've Developed

By building a two-layer quality architecture, you've learned to think about validation like a systems engineer. You understand that validation is not binary. Rules catch breadth. AI catches depth. Humans catch edge cases. You understand stratified sampling and risk-based approaches. You understand that trust must be measured, not assumed. You understand the economics of quality—when to invest in semantic validation and when rule-based is enough. You can make the argument to leadership for why semantic validation is justified based on cost per error prevented.

## What's Next

You've built a data quality layer that catches what rule-based tools miss. You've implemented sampling strategies and human review workflows. But now you're scaling these systems across dozens of pipelines, and you've discovered a new problem: cost.

Your AI validation is catching errors. It's improving data quality. But the bill keeps growing. Your semantic validator costs $10K per month. Your human review team costs $50K per month. Your infrastructure costs multiply. Meanwhile, leadership asks: is this worth it? Are we preventing enough errors to justify the spend? How do you make that case?

That's when you need to think like a FinOps engineer. You need to understand unit economics. You need to know the cost per validation, cost per error prevented, and cost per business outcome. You need to make data quality investment decisions based on money, not just good intentions.

**Next article: "FinOps for AI Pipelines"** – where we build cost models for AI systems, learn when to optimize for cost versus quality, and make the business case for AI infrastructure to skeptical leadership.

---

## GitHub

All architecture diagrams, cost models, and the complete 8-part series are available in the repository:

**[github.com/jay-jain-10/de-in-ai-series](https://github.com/jay-jain-10/de-in-ai-series)**

The repo contains all 8 articles as markdown with architecture diagrams, AWS/GCP cost breakdowns, trade-off analyses, and DE fundamentals sections. Fork it and adapt the patterns to your own cloud environment.

*This is Part 6 of 8. Next up → [Part 7: FinOps for AI Pipelines](https://github.com/jay-jain-10/de-in-ai-series/blob/main/articles/article-07-cost-engineering.md) — where you learn to cut 89.5% of your AI costs.*
