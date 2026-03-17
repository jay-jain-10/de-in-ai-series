# Designing for Model Heterogeneity: When One Model Isn't Enough

**Part 4 of the "Data Engineering in the Age of AI" Series**

> **The Problem:** Single-model approaches don't scale. Different tasks have different accuracy/cost/latency trade-offs. Sentiment needs speed, toxicity needs accuracy, aspects need grounding. Running the same expensive model on all tasks wastes 10x in API costs; using cheap models everywhere sacrifices quality.
>
> **Who This Is For:** Lead data engineers and data architects building production AI pipelines on AWS/GCP
>
> **What You'll Walk Away With:** Four orchestration patterns (router, chain, fan-out, cascade) that route tasks to different models based on complexity, confidence, and cost—optimizing the cost-latency-accuracy triangle without sacrificing reliability.

Product asks for a feature: analyze product reviews. Sentiment (positive/negative/neutral) for the dashboard. Aspect extraction (which features are mentioned) so users can search by product attribute. Toxicity detection to flag abusive reviews before they go live.

Your first instinct: call Claude once, get three outputs. Simple.

Your second instinct: that won't work, and you should know why.

A week into implementation, you're debugging. Sentiment is fast (Claude Haiku, 200ms). Toxicity detection needs accuracy (Claude Opus, 2 seconds). Aspect extraction hallucinates without grounding (you're embedding each review, searching a vector store, then asking Claude to extract only the aspects that exist in the search results). Your orchestrator calls all three in series. Total latency: 3–4 seconds per review. At 100K reviews/day, that's 100K requests × 3 seconds = 83 hours of compute. Your cost is 10x what you budgeted. The dashboard is unusably slow.

This is where data engineers confront the reality: you're not just calling an API anymore. You're orchestrating a distributed system with multiple models, each with different cost, latency, and accuracy characteristics. The optimization problem is NP-hard. You need architecture.

## The Cost-Latency-Accuracy Triangle

Every AI model lives in a three-dimensional trade-off space:

- **Accuracy**: How correct are the results?
- **Latency**: How fast does it respond?
- **Cost**: How much does it cost per request?

You can optimize for any two of these, but not all three.

Claude Haiku: fast (200ms), cheap ($0.0005 per classification), but less accurate (88% vs. 92% for Sonnet).

Claude Sonnet: medium speed (800ms), medium cost ($0.002 per classification), good accuracy (92%).

Claude Opus: slow (1.5 seconds), expensive ($0.008 per classification), best accuracy (95%).

An embedding model (Voyage or OpenAI) + vector search: very fast (100ms), cheap ($0.0001), but only works for retrieval, not reasoning.

Your job as an architect is deciding: which task gets which model, and why?

## Four Orchestration Patterns

There are four common patterns. Each has trade-offs.

```
┌─────────────────────────────────────────────────────────────────┐
│              FOUR ORCHESTRATION PATTERNS                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Pattern 1: ROUTER              Pattern 2: CHAIN                │
│  ┌───────┐                      ┌──────┐                        │
│  │Review │──▶ Complexity ──┐    │Review│                        │
│  └───────┘    Score        │    └──┬───┘                        │
│               ┌────────────┤       ▼                            │
│         Simple│     Complex│    ┌──────┐  neg   ┌──────┐       │
│               ▼            ▼    │Haiku │──────▶│Sonnet│       │
│          ┌──────┐    ┌──────┐  │Sentim│  pos   │Aspect│       │
│          │Haiku │    │Sonnet│  └──────┘──▶skip │Extrc.│       │
│          │ ALL  │    │ ALL  │               ▼   └──┬───┘       │
│          └──────┘    └──────┘              done     │unsafe     │
│                                                     ▼           │
│  Pattern 3: FAN-OUT/FAN-IN     Pattern 4:     ┌──────┐        │
│  ┌───────┐                     FALLBACK       │ Opus │        │
│  │Review │──┬──▶ Haiku ───┐    CASCADE        │Toxic.│        │
│  └───────┘  │   (Sentim)  │                   └──────┘        │
│             ├──▶ Sonnet ──┤    ┌──────┐                       │
│             │   (Aspect)  │    │Haiku │──conf≥0.7──▶ Done     │
│             ├──▶ Opus  ───┤    └──┬───┘                       │
│             │   (Toxic)   │       │conf<0.7                    │
│             └─────────────┤       ▼                            │
│                     ┌─────▼────┐ ┌──────┐                     │
│                     │  Merge   │ │Sonnet│──conf≥0.7──▶ Done  │
│                     │ Results  │ └──┬───┘                     │
│                     └──────────┘    │fail                      │
│                                     ▼                          │
│                                  ┌──────┐                     │
│                                  │Human │                     │
│                                  │Review│                     │
│                                  └──────┘                     │
└─────────────────────────────────────────────────────────────────┘
```

**Pattern 1: Router (Simple Decision Tree)**

A lightweight model (Haiku) reads the input and decides which downstream model to use. For a review analysis pipeline:

The router reads the 200-word product review and classifies it as simple or complex:
- Simple: "This product is great. 5 stars." → Use Haiku for all three tasks (sentiment, aspects, toxicity)
- Complex: "The battery life is good but the software is buggy and the customer service refused to help when it broke." → Use Sonnet for all three tasks

The router uses a simple heuristic: word count + keyword detection. Reviews under 100 words are simple. Reviews over 300 words or containing certain keywords (lawsuit, discrimination, explicit language) are complex.

This reduces your compute cost. 70% of reviews are simple (Haiku, $0.0005), 30% are complex (Sonnet, $0.002). Blended cost: $0.00085 per review. Total: $0.085 per 100 reviews.

Trade-off: The router is imperfect. Some complex reviews get Haiku and produce inaccurate results. Some simple reviews get Sonnet and waste money. But the system is simple and cost-effective.

**Pattern 2: Chain (Sequential Processing)**

Tasks depend on each other's outputs. Aspect extraction requires sentiment context. Toxicity detection might need aspect information.

The chain runs sequentially:
1. Sentiment classification (Haiku, fast)
2. If sentiment is negative, run aspect extraction (Sonnet)
3. If aspects include unsafe language, run toxicity check (Opus)

This reduces the number of expensive calls. If 60% of reviews are positive (skip aspect extraction), and 80% of negative reviews don't need toxicity checks, you save significant cost.

Cost: 100 reviews: (100 × $0.0005 sentiment) + (40 × $0.002 aspect extraction) + (8 × $0.008 toxicity) = $0.05 + $0.08 + $0.064 = $0.194 for 100 reviews.

Trade-off: Sequential execution is slower (3–4 seconds per review). If a review needs all three tasks, you wait for sentiment → aspects → toxicity serially. But the cost savings are significant.

**Pattern 3: Fan-Out / Fan-In (Parallel Processing)**

All tasks run in parallel. A router sends a single review to three separate AI tasks simultaneously. They complete independently. Results are merged.

Latency: max(sentiment_time, aspect_time, toxicity_time) = max(200ms, 1000ms, 1500ms) = 1.5 seconds.

This is faster than sequential but more expensive (all three models run on every review).

Trade-off: Better latency, higher cost. Useful when latency is critical (real-time dashboard updates).

**Pattern 4: Fallback Cascade (Graceful Degradation)**

Start with the cheapest model. If confidence is too low, escalate to a more expensive model.

For aspect extraction:
1. Try Haiku with few-shot examples (cheap, might fail)
2. If confidence < 0.7, retry with Sonnet (more expensive but more reliable)
3. If that fails, return a fallback result and queue for human review

This minimizes cost while maintaining quality. Haiku handles 85% of reviews perfectly. Sonnet handles 14% of the difficult ones. 1% are queued for humans.

Cost: (85 × $0.0005) + (14 × $0.002) + (1 × human_review_cost) = $0.0425 + $0.028 + $1 = $1.0705 per 100 reviews.

Trade-off: More complex orchestration logic. But you get cost efficiency plus quality.

## The Architecture: Model Router and Decision Logic

Here's where the architecture starts. The model router is the critical component. It decides which model to use for which task.

The code implements this as a YAML config that maps tasks to models:

```yaml
tasks:
  sentiment:
    router: complexity_score  # Use complexity to decide Haiku vs Sonnet
    default_model: haiku
    fallback_model: sonnet
    complexity_threshold: 0.6
  aspect_extraction:
    router: embedding_similarity  # Use vector similarity to decide whether to use embedding model first
    default_model: haiku_with_rag  # Haiku + retrieval-augmented generation
    fallback_model: sonnet_with_rag
    embedding_threshold: 0.8
  toxicity_detection:
    router: keyword_urgency  # Check for urgent safety issues
    default_model: haiku
    fallback_model: opus
    safety_keywords: [...urgent keywords...]
```

The router reads this config and implements the decision logic:
- For sentiment, calculate a complexity score (word count, sentence complexity, keyword presence)
- If score > threshold, use Sonnet; otherwise Haiku
- For aspect extraction, embed the review and search a vector store of known aspects
- If top-k results have high similarity (>0.8), use the retrieval-augmented approach; otherwise use standard extraction
- For toxicity, scan for keywords that demand immediate attention
- If found, use Opus; otherwise Haiku

This logic is testable. Write golden datasets with reviews you know are complex/simple, and verify the router makes the right calls.

## Circuit Breaker Pattern

When any model fails, you need graceful degradation. The circuit breaker pattern prevents cascading failures:

If Claude API is rate-limited or timing out:
1. Circuit breaker opens (stops sending requests)
2. For sentiment, return fallback: {"sentiment": "unknown", "confidence": 0.0, "needs_review": true}
3. Queue the review for retry after 1 hour
4. Periodically test if the circuit should close (try 1% of requests)
5. When it succeeds, gradually ramp back to 100%

This pattern is implemented as middleware: before calling Claude, check the circuit breaker state. If open, return fallback. If closed, make the call. Track consecutive failures per model. Open the circuit after 5 consecutive failures. Test recovery every 60 seconds.

This requires per-model health tracking. You log:
- Consecutive failures
- Time since last successful request
- If circuit is open/closed
- Fallback rate (how many requests returned fallback values)

Alert the team if fallback rate exceeds 5% (something is wrong upstream).

## Caching Layer: Redis

If you're running 100K reviews/day, you'll get duplicates. A review of the same product might be submitted twice. A competitor might scrape your competitor's site and submit their reviews.

Redis caching prevents duplicate processing. Before calling Claude, hash the review content and check Redis:
- If found (hit), return cached result (0ms latency)
- If not found (miss), call Claude, cache the result with 30-day TTL, return result

Cache hit rate typically 10–15% at scale. That's 10–15K free requests/day. At e-commerce scale, that's $5–10/day in savings. $2,000/year for a simple cache.

The cache key is important: don't use review_id (every submission is unique). Use content_hash(review_text). This catches true duplicates even if submitted by different users.

Log cache hit/miss rates. If hit rate drops below 5%, investigate. You might be seeing new bot submissions, or competitors' review data, indicating something changed.

## Dagster Asset Graph with Model-Aware I/O Managers

Orchestrating this with Dagster gives you fine-grained control:

```
review_text
  ↓
[sentiment] → sentiment_classifications
  ↓
[aspect_extraction] → aspect_classifications (only if sentiment = negative)
  ↓
[toxicity_detection] → toxicity_classifications (only if aspects flagged)
  ↓
[review_aggregation] → final_review_enrichment
```

Each box is a Dagster asset with its own resource requirements and retry policy:
- sentiment: timeout=1 second, retry 2x on timeout, max_concurrency=1000
- aspect_extraction: timeout=3 seconds, retry 1x, max_concurrency=100 (resource-limited)
- toxicity_detection: timeout=5 seconds, retry 1x, max_concurrency=50 (safety-critical, slow)
- aggregation: timeout=2 seconds, retry 3x (stitches results together, must succeed)

Dagster materializes assets on a schedule (daily at 2am), respecting the dependency graph. If sentiment fails for a review, aspect extraction and toxicity don't run for that review—it goes to a retry queue.

I/O managers handle writing results. By default, reviews go to BigQuery. But if a review's toxicity confidence < 0.7, the I/O manager routes it to a separate "needs_human_review" table. This is configuration, not code: the orchestrator makes routing decisions based on output data.

## Cost Tracking Middleware

Every API call is logged with cost metadata:

```
timestamp: 2024-03-10T14:23:45Z
task: sentiment_classification
model: haiku
review_id: rev_12345
input_tokens: 120
output_tokens: 15
cost_usd: 0.00011
latency_ms: 245
model_decision: router_complexity_score=0.35, used_haiku_because < 0.6
cache_hit: false
confidence: 0.92
```

These logs flow to CloudWatch/Cloud Logging. A background job aggregates by task and model:

```
Task: sentiment
  haiku: 85000 calls, $42.50, avg_latency_200ms
  sonnet: 15000 calls, $30.00, avg_latency_800ms
Task: aspect_extraction
  haiku_with_rag: 60000 calls, $30.00, avg_latency_400ms
  sonnet_with_rag: 40000 calls, $80.00, avg_latency_1200ms
Total: 200000 calls, $182.50 (~$0.0009 per review)
```

This visibility lets you optimize. If sentiment accuracy is 91% with Haiku and 93% with Sonnet, but cost is 4x higher, should you pay for the 2% accuracy gain? The cost data informs the decision.

## Cloud Architecture: Where This Runs

### AWS: API Gateway + Lambda Router + ECS Workers

**API Gateway + Lambda**: Incoming reviews hit API Gateway (handles traffic shaping, rate limiting). Lambda router function reads the review, makes routing decisions (which model for which task), publishes messages to SQS.

Why not run the router in ECS? Lambda cold starts are 200–500ms. The router decision logic is lightweight (embedding lookup, complexity score calculation). Lambda is 10x cheaper and fast enough. If you had complex, long-running logic in the router, ECS would be better.

**ElastiCache Redis**: The cache layer sits between API Gateway and the router. Cache hits bypass Lambda entirely.

**ECS Fargate**: Workers consume SQS messages. Each worker calls Claude (or embedding model, or whatever). Workers are task-specific: sentiment_workers, aspect_workers, toxicity_workers. This lets you scale each independently. If toxicity detection needs higher concurrency, scale that fleet without scaling the others.

**CloudWatch**: Logs every API call with tokens, cost, latency. Metrics dashboard shows cost per task per model.

**RDS or DynamoDB**: Store results and metadata. Results also go to S3 for long-term backup.

**Latency Profiles by Pattern (p50 / p95 / p99)**:

| Pattern | p50 | p95 | p99 | Best For |
|---------|-----|-----|-----|----------|
| Router | 250ms | 900ms | 1.8s | Cost optimization with acceptable latency |
| Chain | 800ms | 2.5s | 4.2s | Dependent tasks where early exit saves cost |
| Fan-Out | 1.5s | 2.0s | 3.0s | Latency-sensitive with independent tasks |
| Cascade | 220ms | 950ms | 2.5s | Cost-first with quality guarantee |

The cascade pattern has the best p50 because 85% of requests complete on the first (cheapest) model. But p99 is worse because the remaining 15% retry on progressively slower models.

**Cost structure**:
- API Gateway: $3.50/million requests = $0.35 for 100K requests/day
- Lambda router: $0.20/million invocations = $0.002
- ElastiCache Redis: $0.02/hour (small cluster) = ~$15/month
- ECS Fargate: ~$0.04/hour per task = $30/month for sustained workers
- Claude API: ~$0.0009 per review = $0.09 for 100K reviews
- CloudWatch: ~$20/month

**Total: ~$100/month for infrastructure + ~$90/month for Claude = $190/month for 100K reviews/day.**

### GCP: Cloud Endpoints + Cloud Run Router + Cloud Run Workers

**Cloud Endpoints**: Google's API management layer. Similar to API Gateway.

**Cloud Run**: For the router (lightweight, auto-scaling) and workers. Cloud Run scales to zero when idle, so if you only process reviews daily, you only pay for the time you're processing (not 24/7 base cost like ECS).

**Memorystore Redis**: Cache layer. $0.03/hour for small instance.

**Cloud Tasks**: GCP's job queue. Equivalent to SQS but tighter integration with Cloud Run.

**Cloud Logging + Cloud Monitoring**: Logs and metrics.

**Cost**: Similar to AWS (~$150–200/month for infrastructure + Claude API).

### Trade-Off: Bedrock / Vertex AI vs. Direct API

You could use:
- **AWS Bedrock**: Managed hosting for Claude on AWS infrastructure
- **GCP Vertex AI**: Managed hosting for Claude on GCP infrastructure

Bedrock and Vertex AI are "managed model services." Benefits:
- No rate limiting hassles (they manage rate limits)
- Integrated billing with your AWS/GCP account
- Fine-tuning available (customize Claude on your data)

Drawbacks:
- Higher latency (API calls go through another layer)
- Fewer model options (not all Claude versions available immediately)
- Higher cost (managed service premium, typically 20–30% markup)

For this use case, direct Claude API is better:
- Latency is lower (direct to Anthropic's infrastructure)
- You get all Claude models (Haiku, Sonnet, Opus) immediately
- Cost is lower
- Rate limiting is manageable (your router already handles it)

Use Bedrock / Vertex AI when: you're fine-tuning models on your own data, or you need the managed service operational simplicity.

## When One Model IS Enough (Avoid Over-Engineering)

Before I hand you a blueprint to build a distributed system, let me be direct: the router pattern, fallback cascade, and circuit breaker are *overkill* if you have simple requirements.

**If all your reviews are simple sentiment classification**: One model, one prompt, done. Use Haiku. Cost $0.0005 per review. No orchestration needed. Call Claude synchronously, get result, load to warehouse.

**If you need 99.9% accuracy on everything**: Just use Sonnet for everything. One model, straightforward. Cost $0.002 per review. The extra simplicity might be worth the cost difference. You avoid the router logic, the caching layer, the separate task tracking.

**If you have <1000 requests/day**: No caching needed. No circuit breaker needed. KISS (keep it simple, stupid). Synchronous processing is fine. One task queue. One model. Done.

**If latency requirements are >5 seconds**: Batch processing is acceptable. You don't need real-time responses. Airflow daily run works perfectly.

The router pattern, fallback cascade, and cost tracking become necessary when:
- You process 100K+ requests/day (cost differences between models matter; 10% savings = thousands/month)
- You have heterogeneous tasks (sentiment vs. toxicity vs. aspect extraction—different accuracy/speed requirements)
- You need <2 second latency (you can't afford serial processing; parallel fan-out is required)
- You want to optimize cost without sacrificing quality (cheaper models for easy tasks, expensive models for hard ones)

Consider the e-commerce company: 100K reviews/day. Using Sonnet for everything = $200/day in API costs. Using the router pattern with Haiku for 70% and Sonnet for 30% = $42/day. That's $158 saved per day, or $4,740/month. The router logic is worth it.

For a smaller company with 5K reviews/day using Haiku everywhere, the optimization gains are only $237/month. The added operational complexity might not justify it. Call a single model, keep it simple.

The key: don't optimize prematurely. Start simple. Measure. When cost or latency becomes a problem, then add routing logic.

## Data Engineering Fundamentals: Multi-Model Patterns

Now let's shift perspective. The orchestration patterns above are algorithms. But underneath them are data engineering patterns that matter when you're building this at scale.

**Idempotency: The Foundation of Reliable Multi-Model Processing**

Each review is processed exactly once per model version. In your results table, the primary key is composite: (review_id, model_version, task_type). This prevents duplicates.

When your pipeline re-runs (because you deployed a new model version, or because yesterday's run partially failed), you update existing results via MERGE rather than inserting new rows. If review_id 12345 was processed by Haiku v1.3 for sentiment, and you re-run with Haiku v1.4, the pipeline executes:

```sql
MERGE INTO sentiment_results t
USING new_results s
ON t.review_id = s.review_id AND t.model_version = s.model_version AND t.task_type = s.task_type
WHEN MATCHED THEN UPDATE SET t.confidence = s.confidence, t.result = s.result
WHEN NOT MATCHED THEN INSERT (review_id, model_version, task_type, result, confidence)
  VALUES (s.review_id, s.model_version, s.task_type, s.result, s.confidence)
```

This is idempotency: you can safely re-run the entire pipeline multiple times. The table state converges to the same result. No duplicates. No cascading failures.

**Data Contracts Between Models**

When the router sends a review to the sentiment model, the contract is explicit: input is raw text (UTF-8 encoded, max 5000 chars). Output is JSON: {sentiment, confidence, reasoning}. Both sides agree on schema, format, and edge cases (what if the review is empty? What if it's in a language the model doesn't support?).

When the chain passes sentiment output to the aspect extractor, the contract is: input includes sentiment context (the sentiment result becomes a field in the prompt). The aspect extractor reads this field and uses it to ground its responses. If the sentiment model omits the field, the aspect extractor fails loudly (validate on input).

These inter-model contracts are enforced via schema validation. Before calling the downstream task, validate that the upstream result matches the expected schema:

```python
sentiment_schema = {
  "sentiment": one_of(["positive", "negative", "neutral"]),
  "confidence": float_between(0, 1),
  "reasoning": str_max_length(500)
}

def validate_sentiment_output(result):
  for field, validator in sentiment_schema.items():
    assert field in result, f"Missing field: {field}"
    validator(result[field])
```

When validation fails, log the breach, increment a metric (contract_violations), and route to a dead letter queue (DLQ). A human reviewer inspects it. Contracts are versioned; when you change a schema, old results still validate against the old version.

**Circuit Breaker as Data Quality Pattern**

The circuit breaker isn't just API resilience—it's a data quality pattern. When the circuit opens (Claude API is rate-limited or timing out), fallback values are tagged in the data:

```json
{
  "review_id": "rev_12345",
  "task": "sentiment",
  "result": null,
  "confidence": 0.0,
  "fallback": true,
  "failure_reason": "circuit_open_rate_limit"
}
```

The fallback flag tells downstream systems: this is degraded data. Downstream dbt models filter these out of production tables. They live in a separate _fallback table for review.

The fallback_rate metric is your canary: if 5% of results are fallbacks, the system is degraded. If 20% are fallbacks, something broke upstream. Alert when fallback_rate exceeds threshold.

**Cost as Metadata**

Every API call produces cost metadata. This gets stored alongside the classification result:

```json
{
  "review_id": "rev_12345",
  "task": "sentiment",
  "model": "haiku",
  "model_version": "1.3",
  "input_tokens": 120,
  "output_tokens": 15,
  "cost_usd": 0.00011,
  "latency_ms": 245
}
```

This metadata flows to your data warehouse. A dbt model computes aggregations:

```sql
SELECT
  task,
  model,
  COUNT(*) as call_count,
  SUM(cost_usd) as total_cost,
  AVG(cost_usd) as avg_cost_per_call,
  PERCENTILE_CONT(latency_ms, 0.95) as p95_latency
FROM model_calls
GROUP BY task, model
```

Now you can detect cost anomalies. If Opus suddenly processes 60% of reviews instead of 5%, something broke in your router logic. An alert fires. You investigate (did a model version change? Did a config get deployed wrong?) and remediate.

Cost data lets you ask: "Did we save money by switching from Sonnet to Haiku for aspect extraction?" Answer: compare total_cost before and after the change.

**Lineage Across Models**

A single review might be processed by 3 models in sequence. The lineage chain is:

```
review_id=rev_12345
  ├─ sentiment_result (Haiku v1.3, latency=245ms, cost=$0.0001)
  └─ aspect_result (Sonnet v2.1, latency=1200ms, cost=$0.002)
      └─ toxicity_result (Opus v1.0, latency=1500ms, cost=$0.008)
```

This full lineage is stored as structured data (parent_id, child_id, model, version, cost) in your warehouse. Now you can query: "Which reviews went to Opus? What's the total cost impact?" or "For reviews that entered Opus, what was the root cause? (low confidence from Sonnet)"

In dbt, you build views that trace this lineage:

```sql
WITH sentiment_base AS (
  SELECT review_id, model, version, cost_usd FROM sentiment_results
),
aspect_base AS (
  SELECT review_id, model, version, cost_usd FROM aspect_results
),
lineage AS (
  SELECT
    s.review_id,
    s.model as sentiment_model,
    a.model as aspect_model,
    s.cost_usd + COALESCE(a.cost_usd, 0) as total_cost,
    CASE WHEN a.review_id IS NOT NULL THEN 1 ELSE 0 END as aspect_executed
  FROM sentiment_base s
  LEFT JOIN aspect_base a ON s.review_id = a.review_id
)
SELECT * FROM lineage
```

Now you can analyze: how many reviews triggered aspect extraction (20%)? What was the marginal cost? (0.002 * 0.2 = $0.0004 per review). Is it worth keeping aspect extraction if only 20% of reviews trigger it?

## Skills Gained

Building this teaches:

- **Distributed systems thinking**: Orchestrating multiple services with different characteristics
- **Cost as a first-class metric**: Making architectural decisions based on cost, not just functionality
- **Observability at scale**: Tracking cost, latency, and quality across multiple models
- **Graceful degradation**: Circuit breakers, fallbacks, and retry patterns
- **Configuration-driven routing**: Making orchestration decisions via YAML, not code

## The Series So Far

We've covered:
- **Article 1**: AI as a transformation stage (fintech, 50K tickets/month)
- **Article 2**: Unstructured data extraction (legal-tech, 10K contracts/quarter)
- **Article 3**: Prompt governance (15 pipelines, shared infrastructure)
- **Article 4**: Model orchestration (e-commerce, 100K reviews/day)

You've learned to architect AI pipelines, handle unstructured data at scale, govern prompts like infrastructure, and orchestrate multiple models. But we haven't talked about what happens when your pipeline must process data in real time. When batch daily runs aren't fast enough.

That's coming in Part 5: Real-Time AI Streams.

---

## Code & Resources

**GitHub Repository:** [github.com/jay-jain-10/de-in-ai-series](https://github.com/jay-jain-10/de-in-ai-series)

**What this article covers:** Four orchestration patterns (Router, Chain, Fan-Out/Fan-In, Fallback Cascade) for routing tasks to different models based on complexity and cost, processing 100K product reviews/day at ~$190/month.

**What's in the repo:**
- `articles/` — All 8 articles in this series as markdown, each with architecture diagrams, AWS/GCP cost breakdowns, trade-off analyses, and DE fundamentals sections
- `README.md` — Series overview with a summary table showing what problem each article solves and the key architecture pattern

**Series reading order:** This is Part 4 of 8. Article 3 governed individual prompts. This article orchestrates multiple models in batch. Next: Article 5 brings AI into real-time streaming with latency budgets and hybrid architectures. Read the full series overview in the [README](https://github.com/jay-jain-10/de-in-ai-series).
