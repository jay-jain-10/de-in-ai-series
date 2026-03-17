# Prompt Governance Is the New Schema Governance

**Part 3 of the "Data Engineering in the Age of AI" Series**

> **The Problem:** Prompts are treated as ad-hoc strings instead of versioned, tested production code. Small prompt iterations accumulate silently, drifting pipeline behavior until data quality degrades weeks later—undetected by row counts or schema validation.
>
> **Who This Is For:** Lead data engineers and data architects building production AI pipelines on AWS/GCP
>
> **What You'll Walk Away With:** A prompt governance system with Git-based versioning, golden dataset testing, CI/CD checks, and production monitoring that detects prompt drift within hours instead of weeks.

It's Tuesday morning. Your data quality dashboard shows a sudden drop in data completeness across six tables. The downstream ML team is panicking. You dig into the DAG logs.

Nothing failed. All jobs ran successfully. Data loaded cleanly. But something is systematically wrong. You trace back to the AI enrichment stage. Someone on the product team updated a prompt Friday afternoon: "Made it more concise," they said. Three days later, you realize: that prompt change silently broke every downstream table because the output schema changed.

A field that was previously always a list is now sometimes a single string. Sometimes null. Your dbt models that expected a specific JSON structure started producing NULLs. Not errors—just data quality degradation spreading through your warehouse.

This is when you learn the hardest lesson about operating AI pipelines at scale: prompts are not developer notes. They're specifications. They're production infrastructure. They deserve the same governance—versioning, testing, code review, deployment controls—that you give to database schemas.

## The Parallel: Schema Evolution and Prompt Evolution

Here's an uncomfortable truth: **a prompt change is a schema change.**

When you alter a database schema—drop a column, widen a field, add a NOT NULL constraint—you own the downstream impact. You notify consumers. You have a deployment plan. You test rollback. The change is explicit and auditable.

When someone changes a prompt, the output schema changes. The structure of JSON, the presence of fields, the format of values—all cascade downstream. It's functionally identical to a schema migration. But it's *invisible*.

A schema change is hard to miss: "ALTER TABLE drop_column revenue" is obvious. A prompt change hides in a line of Python:

Old: "Extract vendor name, payment terms, and currency. Return JSON with fields name, terms_days, currency."

New: "Extract vendor info and terms. Return JSON."

That's a real change I've seen. The new prompt doesn't specify the field names. Claude now returns vendor_name or name or vendor_company (it rotates). The downstream dbt model expects vendor_name. 50% of rows now have NULL in that column. No error. Silent data corruption.

This is "prompt drift": small changes accumulate without visibility, until one day your dashboards are subtly wrong.

## Prompt Drift: The Boiling Frog Problem

At the fintech company from Article 1, the support ticket classifier worked great for three months. Accuracy was 92%. Then someone said: "Let's add context about the customer's history. That might improve escalation prediction."

So they updated the prompt to include the customer's previous tickets. Reasonable, right? The model started seeing patterns it couldn't see before. Escalation accuracy improved to 94%.

Six weeks later, the VP of Support asked: why are we escalating 3x more tickets than we used to? The absolute number jumped from 8% to 24% of daily intake. They checked the data. Nothing changed in the business. The tickets weren't actually harder. The classifier just started marking more things as escalation-worthy.

A prompt change meant to improve accuracy had drifted into changing what "escalation" meant. Without a mechanism to detect it, the drift went unnoticed for weeks.

This is the boiling frog problem. Small prompt iterations—adding detail here, clarifying there—compound over time. Each change seems reasonable in isolation. Collectively, they transform your pipeline's behavior.

## The Solution: Prompts as Infrastructure

The real solution is treating prompts exactly like database schemas. Here's the architecture:

```
┌──────────────────────────────────────────────────────────────────────┐
│                    PROMPT GOVERNANCE PIPELINE                        │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────┐    ┌───────────────┐    ┌──────────────────────────┐  │
│  │  Git Repo │───▶│  CI/CD        │───▶│  Golden Dataset Tests    │  │
│  │  (YAML   │    │  (CodeBuild / │    │  (pytest + Claude API)   │  │
│  │  Registry)│    │  Cloud Build) │    │                          │  │
│  └──────────┘    └───────────────┘    │  • Schema conformance    │  │
│       │                               │  • Confidence ranges     │  │
│       │ version                       │  • Regression detection  │  │
│       │ tagged                        │  • Hallucination check   │  │
│       │                               └────────────┬─────────────┘  │
│       ▼                                            │                 │
│  ┌──────────┐    ┌───────────────┐    ┌────────────▼─────────────┐  │
│  │ S3 / GCS │◀───│  Deploy       │◀───│  Pass? ──▶ Tag + Deploy │  │
│  │ Registry │    │  (Airflow     │    │  Fail? ──▶ Block PR     │  │
│  │ (Runtime)│    │   reads at    │    └──────────────────────────┘  │
│  └────┬─────┘    │   runtime)    │                                  │
│       │          └───────────────┘                                  │
│       ▼                                                             │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              PRODUCTION MONITORING                            │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐│   │
│  │  │ Output   │  │Confidence│  │ Schema   │  │ Hallucination││   │
│  │  │Divergence│  │Degradation│ │Compliance│  │ Rate         ││   │
│  │  │ <10%     │  │ <5% drop │  │ >95%     │  │ <3%          ││   │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────────┘│   │
│  │         ▼            ▼             ▼              ▼         │   │
│  │  ┌────────────────────────────────────────────────────────┐ │   │
│  │  │  CloudWatch / Cloud Monitoring → SNS Alerts            │ │   │
│  │  └────────────────────────────────────────────────────────┘ │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

**Stage 1: Prompt Registry**

Prompts live in a YAML file (not hardcoded in Python). The registry looks like:

```
sentiment_classifier:
  version: 1.3
  model: claude-3-5-haiku
  system_prompt: |
    You are a customer support sentiment analyzer. Classify sentiment as positive, neutral, negative, or frustrated.
    Return JSON with sentiment field (one of: positive, neutral, negative, frustrated) and confidence (0.0-1.0).
  validation_schema:
    type: object
    properties:
      sentiment:
        type: string
        enum: [positive, neutral, negative, frustrated]
      confidence:
        type: number
        minimum: 0
        maximum: 1
    required: [sentiment, confidence]

escalation_classifier:
  version: 2.1
  model: claude-3-5-sonnet
  system_prompt: |
    You are an escalation risk evaluator...
```

This registry is versioned in Git. Every change goes through code review. When you want to update a prompt, you:
1. Create a PR with the new prompt and increment the version
2. Run automated tests (golden datasets, regression detection)
3. Wait for review
4. Merge to main
5. Deploy the new version

Your Airflow/Composer DAG loads prompts from the registry at runtime. The classifier reads `sentiment_classifier:version=1.3` from the registry, not from hardcoded Python strings. This indirection is critical.

**Stage 2: Testing Framework**

Each prompt has a golden dataset—a set of test inputs with expected outputs. For the sentiment classifier:

```
Input: "Your app crashed and lost my data"
Expected output: { sentiment: "frustrated", confidence: >= 0.8 }

Input: "Great product, love using it"
Expected output: { sentiment: "positive", confidence: >= 0.8 }

Input: "It's fine"
Expected output: { sentiment: "neutral", confidence: >= 0.6 }
```

When you propose a new prompt version, your test harness runs it against the golden dataset. It compares the new results to the baseline. If the new version fails more than 5% of golden cases, the test blocks the PR merge. If it passes >5% more cases, you get a green flag.

This is pytest with fixtures. Golden datasets are loaded from JSON. Test assertions compare:
- Exact match on output schema
- Confidence scores are in expected ranges
- Hallucinations are detected (extracted values appear in source text)
- No regressions on previously-passing cases

You run this test suite on every PR. Some teams run golden dataset tests continuously in production (sample 1% of requests daily, compare against golden baseline). This catches prompt drift immediately.

**Stage 3: Versioning and Lineage**

The Snowflake table that stores classifications includes a `prompt_version` column. Every record knows which prompt version produced it. This enables:

- Reprocessing: When you upgrade the prompt, you can selectively reprocess old records to compare quality
- Lineage: Debugging data quality issues by checking which prompt version produced which rows
- Rollback: If a new prompt is worse, you can revert to the old version and reprocess recent data

Your dbt models test for prompt_version consistency. "All high-confidence escalations should come from prompt_version >=2.0" (because older versions were less accurate). This enforces that old, less-accurate classifications aren't leaking into production.

**Stage 4: CI/CD Pipeline**

Your GitHub Actions pipeline:
1. On every commit to the prompts file, run the test suite (golden datasets)
2. On every PR, block merge if tests fail or confidence degrades >5%
3. On merge to main, tag the commit with version number
4. On release (manually triggered), deploy the new prompt version to MWAA/Cloud Composer

Your Airflow DAG reads the latest prompt version from the registry at the start of each run. It logs which version it used. If you need to rollback, you update the registry to point to the previous version, and the next DAG run uses it. All previous data remains tagged with the version that produced it.

## Organizational Structure: Who Owns Prompts?

Here's where the organizational question hits.

In most companies, prompts are written by product, ML, or data science teams. They're treated as "parameters." Someone tweaks the prompt and pushes it to production without notifying the data engineering team.

You need a different structure:

**Prompts are a shared contract between multiple teams.**

- **Product/ML owns the prompt's intent**: "We want to detect escalation risk so we can route complex tickets to senior agents." They define what the prompt should accomplish.
- **Data Engineering owns the prompt's specification**: "The output JSON must have these fields with these types and these value ranges." They enforce schema and versioning.
- **The data/analytics team owns prompt validation**: "The outputs should have confidence ≥ X for production use. Below that, route to human review."

In practice, this means:
- Prompts live in the data engineering repository, not the data science repository
- Prompt changes require approval from data engineering before deployment
- Every prompt change increments a version number
- Golden datasets are maintained by the team that uses the output (product, ML, analytics)
- PR reviews include someone from each team

This sounds bureaucratic, but it prevents silent failures. When someone wants to change a prompt, they:
1. Propose the change in a PR
2. Run the test suite (golden datasets from the product/ML team)
3. Get sign-off from data engineering
4. Deploy with version tracking and rollback capability

## Parallel to dbt Governance and Version Control Best Practices

This pattern mirrors mature dbt governance (if your company has it).

In production dbt implementations, you don't change a model in production directly. Instead:
- Create a feature branch in git
- Write/modify the dbt model
- Add schema tests (not null, unique, relationships)
- Add generic tests or custom tests in dbt_utils
- Create a PR with the change
- Get code review from another team member
- Run dbt test in CI to ensure tests pass
- Merge to main only after approval
- Deploy to production with documented lineage and impact analysis

Prompt governance follows this identical pattern, except your tests are different:
- Golden dataset regression tests (comparing outputs against known good examples)
- Confidence distribution checks (new version should maintain similar confidence levels)
- Hallucination detection (extracted values should appear in source documents)
- Schema conformance tests (extracted JSON matches expected structure)

The mental model is identical. Prompts *are* code. Your CI/CD pipeline should treat them as such.

The key insight: just as dbt models have data contracts (expected inputs, guaranteed outputs, schema), prompts have data contracts too. A prompt's contract is "given input of type X, return output matching schema Y with confidence Z." When the contract changes, downstream consumers break.

## Organizational Alignment: The Three-Team Model

Here's how successful data organizations structure prompt governance:

**Data Engineering Team**:
- Owns the prompt registry (YAML file in Git)
- Owns the CI/CD pipeline (GitHub Actions tests)
- Enforces Pydantic schema validation
- Maintains the prompt versioning system
- Sets up monitoring for prompt drift

**Product/ML Team**:
- Defines what the prompt should accomplish ("We want to detect escalation risk so senior agents handle complex cases")
- Creates golden datasets (examples where the extraction should succeed/fail)
- Maintains accuracy metrics
- Proposes new prompt changes
- Reviews accuracy improvements

**Data/Analytics Team**:
- Tests downstream impact (does this prompt change break any dashboards?)
- Maintains dbt tests that verify prompt outputs
- Runs reprocessing jobs when prompts change
- Monitors business metrics that depend on prompt outputs

In practice, when someone wants to change a prompt:
1. They file an issue describing the change and business motivation
2. They create a PR with the new prompt version
3. Data engineering runs the test suite; golden datasets must pass
4. Product/ML reviews and approves the change
5. Analytics team checks downstream impact (any dbt models that depend on this?)
6. PR merges; new version is deployed
7. Monitoring tracks accuracy metrics for 7 days
8. If metrics degrade, auto-rollback is triggered

This process sounds bureaucratic, but it prevents the silent data corruption that killed the fintech company's escalation predictions.

## The Real Cost of Prompt Governance

You might be thinking: this sounds like bureaucracy overhead. Is it worth it?

Consider the actual cost of prompt drift at scale:

You have 15 AI pipeline stages across 6 projects. A prompt change in one stage breaks downstream logic. It produces systematically wrong outputs, but they load successfully. Your data quality dashboard doesn't catch it because the checks pass (rows exist, values are in expected ranges, but semantics are wrong).

Downstream, this breaks ML training. The ML team retrains a model on data with corrupted features. The model's accuracy drops by 2%. In production, this costs you 5% fewer correct predictions. For a fintech company operating $10B in daily transaction volume, 2% accuracy loss translates to ~$5M/month in business impact (fewer fraud detections, more chargebacks, higher risk exposure).

The cost of preventing that: 2 hours of review time per prompt change. 8 people × $150/hour × 2 hours = $2400.

The ROI is infinite. You prevent one month of degraded performance, you save millions. The governance overhead is trivial.

Real story: A legal-tech company in 2024 released a prompt change that subtly altered contract interpretation. The new prompt was more "concise" (fewer details). It missed critical liability exclusions in 12% of contracts. Three months passed before anyone noticed. They had to reprocess 30K contracts with the old prompt, manually review edge cases, and contact customers. Total cost: $800K in labor + legal risk. Governance overhead would have been $500.

## Testing Golden Datasets in Practice

Let me give you a concrete example. For the fintech escalation classifier, the golden dataset looks like:

```yaml
test_cases:
  - name: clear_escalation_urgent
    input: "App crashed and I lost $50K. This is unacceptable. Call me NOW."
    expected_escalation_risk: 0.95  # must be >= 0.90 confidence
    expected_urgency: critical

  - name: ambiguous_issue
    input: "The app seems slower today. Not sure if it's my phone or the app."
    expected_escalation_risk: 0.45  # could go either way
    expected_urgency: low
    acceptable_confidence_range: [0.3, 0.6]  # low confidence acceptable here

  - name: regression_old_format
    input: "Account locked. Password reset not working. I'm locked out 24 hours now."
    expected_escalation_risk: 0.98
    note: "This case failed in v0.9; v1.0 must catch it"
```

When you propose a new prompt version, your test harness:
1. Runs the new prompt on all these cases
2. Compares outputs: do they match expectations?
3. For regression cases: does the new version fix the bug?
4. For ambiguous cases: is the confidence still in the acceptable range?
5. Computes a regression score: how many test cases does the new version fail?

If >5% of tests fail, the PR gets blocked. The engineer has to fix the prompt or update the test cases (with careful review).

This is how you prevent silent drift. You write golden datasets in plain English. You version them alongside prompts. You test every change.

## Monitoring: Detecting Prompt Drift in Production

Here's something you won't find in most articles: how to detect when prompt changes break production silently.

Set up continuous monitoring on your classifications. Every day, sample 1% of production requests (randomly). For these sampled requests, log:
- Original input
- Output from the current prompt version
- Output from the previous prompt version (by re-running the old prompt)
- Confidence scores from both
- Schema match (does the new output match the expected schema?)

Then compute metrics:
- **Output divergence**: How often do the new and old prompts produce different results? Expect <5% divergence. If >10%, investigate.
- **Confidence degradation**: Is the new prompt less confident than the old one? New version average confidence should be within 5% of the old baseline.
- **Schema compliance**: What % of outputs match the expected schema? Should be >95%. If <90%, alert immediately.
- **Hallucination rate**: What % of extractions appear in the source text? Track this per extraction type. If hallucination rate increases >3%, rollback.

Log these metrics to CloudWatch/Cloud Monitoring. Create a dashboard. Set alerts:
- Alert if hallucination rate > 5%
- Alert if schema compliance < 90%
- Alert if average confidence drops >0.05 points
- Alert if output divergence > 15% (prompt changed significantly)

This continuous monitoring catches prompt drift within hours, not weeks. When an alert fires, your data team can decide: investigate the change, rollback, or accept the new behavior.

**Monitoring Dashboard Layout**: Three panels. Panel 1 (left): Time series of output divergence rate, 7-day rolling window, with 10% threshold line. Panel 2 (center): Confidence score distribution histogram, current version vs previous version overlay. Panel 3 (right): Schema compliance percentage and hallucination rate as gauges with red/yellow/green zones. Refresh interval: hourly. Data source: CloudWatch custom metrics from the 1% production sample.

## Cloud Architecture: Where Prompt Governance Runs

The prompt governance system I've described requires actual infrastructure. Let's look at how you build this on AWS and GCP.

### AWS Implementation

**S3**: Prompt registry YAML files stored in versioned S3 bucket. Your registry file lives here, not in application code. Cost: minimal ($0.023/GB-month).

**CodePipeline + CodeBuild**: CI/CD for prompt changes. When a PR merges to main, CodePipeline triggers CodeBuild to run your golden dataset tests. CodeBuild spins up a container, executes pytest against your test suite, logs results, and passes/fails the pipeline. Cost: $1 per active pipeline/month + $0.005/build-minute. Typical for prompt governance: $10-20/month.

**Lambda**: Runs the golden dataset test suite (pytest with Claude API calls against your test cases). You allocate 256MB memory and set a 5-minute timeout per test run. When CodeBuild triggers Lambda, it loads the candidate prompt and old prompt from S3, runs both against your golden dataset, compares results, and returns pass/fail. Cost: $5-10/month.

**MWAA (Managed Airflow)**: Your Airflow DAG loads the prompt version from the S3 registry at runtime. Critical design: the DAG does not have prompts hardcoded in the Python code. Instead, it reads `sentiment_classifier:version=1.3` from the registry YAML, fetches that prompt from S3, and uses it. This indirection is what enables zero-downtime rollback. Cost: ~$200/month (same infrastructure as Article 1; you're sharing the Airflow cluster with your other pipeline orchestration).

**DynamoDB**: Stores prompt version metadata (version number, timestamp, author, test results, rollback history). On-demand pricing since write volume is low. Cost: $5-10/month at low write volume.

**CloudWatch**: Monitors your prompt drift metrics (output divergence, confidence degradation, schema compliance, hallucination rate). Custom metrics are $0.30/metric/month. You'll likely track 15-20 metrics = $5-6/month. Alarms alert your team when thresholds are breached.

**SNS**: Sends alerts when prompt drift is detected or golden dataset tests fail. $0.50/million notifications. Cost: <$1/month.

**Total AWS: ~$250-300/month** (mostly MWAA, which is shared infrastructure used by your entire pipeline).

### GCP Implementation

**GCS**: Prompt registry stored in versioned GCS bucket, analogous to S3. Cost: minimal.

**Cloud Build**: CI/CD for prompt changes. Triggered on GitHub PR merge to main. Runs your golden dataset test suite. Pricing: $0.003/build-minute. Cost: $5-15/month.

**Cloud Functions**: Runs the golden dataset tests. HTTP-triggered, executes pytest, compares old vs. new prompt outputs against golden dataset. Cost: $3-8/month.

**Cloud Composer**: Your Airflow environment. DAG loads prompts from GCS registry at runtime, not from code. Cost: ~$220/month (shared infrastructure).

**Firestore**: Stores prompt metadata, test results, version history. Cost: ~$5/month.

**Cloud Monitoring**: Built-in alerting on custom metrics. Included in Cloud Composer pricing.

**Total GCP: ~$250-280/month**

The cost structure is nearly identical between AWS and GCP because both architectures follow the same pattern: a registry system, a CI/CD pipeline, a data warehouse metadata store, and continuous monitoring.

### Design Trade-Offs

**S3/GCS vs. keeping prompts only in Git**

Some teams argue: "Why not just keep prompts in the Git repository? Why add S3/GCS as another system?"

The answer: Git works for version control, but not for runtime reads. If your Airflow DAG reads prompts from Git at runtime (cloning the repo, checking out a specific commit, parsing the YAML), you add latency and a hard dependency on Git availability. If Git is down, your pipeline can't read prompts. S3/GCS is faster and more reliable for runtime reads. The pattern: Git is your source of truth (humans edit here, code review happens here). S3/GCS is your deployment target (production reads from here).

**Lambda/Cloud Functions vs. dedicated service**

Golden dataset tests could run in a dedicated ECS service or Cloud Run service instead of Lambda/Cloud Functions. Lambda is cheaper for infrequent test runs (a few times per week, triggered by PR merges). If you're running tests continuously—every hour, sampling production data—a dedicated service is more cost-effective. For most teams starting out, Lambda/Functions is the right choice.

**DynamoDB/Firestore vs. PostgreSQL**

For simple prompt metadata (version number, timestamp, test results), a key-value store like DynamoDB or Firestore is simpler. If you need complex queries later—join prompt versions with classification accuracy metrics over time, query "which prompts have hallucination rate > 5% in the last 7 days?"—use PostgreSQL or Cloud SQL instead. Start with the simpler option; migrate if your query patterns get complex.

## Implementation Checklist

To implement this at your company:

1. **Extract all hardcoded prompts into a YAML registry**. One file, versioned in Git. Separate registry for each domain (sentiment, extraction, classification).

2. **Create a Pydantic schema for each prompt's output**. Enforce it at runtime. If Claude returns output that doesn't match the schema, retry with a clearer example.

3. **Build a golden dataset test suite**. Use pytest with fixtures. Golden datasets should cover:
   - Edge cases (very long text, special characters, multiple languages)
   - Regression cases (examples where the old prompt excelled that the new one must not regress on)
   - Performance cases (example that are known to be difficult)
   Run this on every PR. Fail if >5% of golden cases fail.

4. **Add a prompt_version column to all tables** that store AI outputs. Track lineage. This column is critical for reprocessing and debugging.

5. **Implement CI/CD checks**. Your GitHub Actions pipeline:
   - On every commit to prompts: Run golden dataset tests
   - On every PR: Run tests + compute regression statistics
   - Block merge if golden dataset test failures >5% or confidence drops >0.05 points
   - On merge to main: Tag commit with version number

6. **Document your prompt governance process**. Make it clear who approves prompt changes (ideally: data engineer + product + ML). What's the review process? How long does it take?

7. **Monitor prompt drift continuously**. Sample 1% of production daily. Compare new vs. old prompt outputs. Alert on divergence, confidence degradation, hallucinations, schema compliance. Dashboard showing these metrics.

8. **Set up rollback capability**. If a new prompt degrades, you should be able to rollback in < 5 minutes. This means:
   - Prompts in a registry (not hardcoded)
   - DAGs load prompts at runtime (not at image build time)
   - Switching to a previous version = updating a YAML pointer

This isn't hard. It's discipline. But the discipline prevents catastrophic failures.

## Data Engineering Fundamentals: Prompt Governance as DE Discipline

Prompt governance isn't just about process—it's about applying core data engineering principles to AI systems. Here's how prompt versioning mirrors classical data engineering patterns:

### Idempotency in Prompt Changes

When you deploy a new prompt version to production, running pipelines must not break mid-execution. Here's how idempotency works:

Your Airflow DAG loads the prompt at runtime start. Specifically, it reads the registry pointer at the moment the DAG begins execution. If the registry points to `sentiment_classifier:version=1.3`, that DAG run uses 1.3 for its entire duration—even if someone updates the registry to 1.4 while the DAG is running.

This ensures each run is consistent and atomic. The next DAG run, starting after the registry update, picks up the new version. Old runs in progress see no change. You achieve zero-downtime deployments without coordinating across running jobs.

Contrast this with hardcoded prompts: if a prompt string lives in your Python code, there's no clean way to swap versions mid-pipeline. You either have to wait for all running jobs to finish (blocking deployments) or update code and risk in-flight inconsistency.

### Exactly-Once Semantics for Reprocessing

When you upgrade a prompt and reprocess historical records with the new version, the MERGE pattern from Article 1 prevents duplicates. Here's why:

Your table has a composite key: `(input_id, prompt_version)`. The MERGE statement:
- Matches on `input_id` and `prompt_version`
- When reprocessing with a new prompt version, you create records with `(input_id, new_version)`
- Existing records with `(input_id, old_version)` stay in place
- No duplicates because the version is part of the key

This achieves exactly-once reprocessing. You can safely replay any input through any prompt version without fear of creating duplicates. The `prompt_version` column acts as a natural partition key.

### Data Contracts for Prompts

In classical data engineering, a data contract specifies: "This table has these columns, with these types, in this range." Downstream teams rely on the contract.

Prompts have contracts too. The Pydantic schema IS the contract:

```python
class SentimentOutput(BaseModel):
    sentiment: Literal["positive", "neutral", "negative", "frustrated"]
    confidence: float
    # Additional fields added in v1.5
    subsentiment: Optional[Literal["angry", "disappointed"]] = None
```

When a prompt changes output format, the Pydantic validator catches the mismatch before data enters the warehouse. This is the contract between AI and downstream consumers. If Claude returns `sentiment_score` instead of `sentiment`, validation fails. You don't silently corrupt data downstream.

Breaking the contract requires explicit communication: increment the schema version, update the validator, update dependent dbt models, inform downstream teams. This prevents the silent cascading failures that plague unversioned AI systems.

### SLA for Prompt Changes

Just as you have SLAs for database availability and pipeline latency, prompt governance has SLAs:

- **Rollback SLA**: <5 minutes. If a new prompt causes problems, you can revert by updating the registry YAML pointer. The next DAG run uses the old version. This assumes your DAG loads prompts at runtime (not at build time).
- **Detection SLA**: Prompt drift detected within 1 hour via continuous sampling. You run 1% of production requests through both old and new prompts hourly, comparing results. If divergence exceeds thresholds, you're alerted.
- **Recovery SLA**: Degraded prompt replaced within 30 minutes. Once drift is detected, you have 30 minutes to rollback or escalate to engineering.

These SLAs are only achievable with the governance structure described in this article. Without versioning, without monitoring, without the ability to swap prompts, these SLAs are impossible.

### Lineage: Tracing Every Row to Its Source Prompt

The `prompt_version` column in every AI-produced row enables complete lineage. Every data point carries metadata: which prompt produced this row?

Combined with Git version history, you can reconstruct the exact prompt text for any historical data point:

```
Row created 2024-11-15 with prompt_version=1.3
→ git log --oneline prompts.yaml | grep v1.3
→ git show 7a3f2c1:prompts.yaml | grep -A 20 "version: 1.3"
→ [Full prompt text that produced this row]
```

This is critical for debugging data quality issues years later. "Why is our customer sentiment distribution skewed in Q3 2024?" Answer: "Because Q3 used prompt v1.2, which had a bug in neutrality detection. v1.3 fixed it in September, so data changes after that."

Lineage transforms AI from a black box into an auditable, traceable system. Every row in your warehouse can be traced back to the exact prompt that created it, and that prompt can be retrieved from version control.

## Skills Gained

Building this teaches:

- **Testing non-deterministic systems**: How to write tests for systems where outputs vary
- **Version control for ML systems**: Managing prompts like code, with proper review and testing
- **Organizational patterns for AI**: How teams coordinate around shared AI infrastructure
- **Data lineage tracking**: Using metadata to understand which processes produced which data

## What's Next

We've solved governance for single prompts. But your pipeline uses 15 prompts across 6 projects. Some are fast and cheap (Haiku). Some are slow and expensive (Sonnet). Some are safety-critical (toxicity detection). Making the right choice about which model to use for which task is its own problem.

That's Part 4: Designing for Model Heterogeneity.

---

## Code & Resources

**GitHub Repository:** [github.com/jay-jain-10/de-in-ai-series](https://github.com/jay-jain-10/de-in-ai-series)

**What this article covers:** Prompt governance as schema governance — treating prompts as versioned production code with Git-based registries, golden dataset testing, CI/CD checks, and automated rollback for multi-pipeline systems.

**What's in the repo:**
- `articles/` — All 8 articles in this series as markdown, each with architecture diagrams, AWS/GCP cost breakdowns, trade-off analyses, and DE fundamentals sections
- `README.md` — Series overview with a summary table showing what problem each article solves and the key architecture pattern

**Series reading order:** This is Part 3 of 8. Article 2 showed extraction pipelines that depend on prompts drifting silently. This article tackles prompt governance with versioning and testing. Next: Article 4 tackles multi-model orchestration with Router, Chain, Fan-Out, and Fallback patterns. Read the full series overview in the [README](https://github.com/jay-jain-10/de-in-ai-series).
