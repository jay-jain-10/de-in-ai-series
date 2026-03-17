# Where AI Meets Event-Driven Architecture
## The Latency Budget Problem and Why Streaming + AI Breaks Your Assumptions

> **The Problem:** Streaming architectures assume predictable latency, but AI inference is variable (200ms-2s), destroying backpressure mechanisms and breaking SLA guarantees. Traditional streaming systems fail when you add language model inference to the critical path.
>
> **Who This Is For:** Lead data engineers and data architects building production AI pipelines on AWS/GCP
>
> **What You'll Walk Away With:** A hybrid streaming + AI architecture that separates the critical path (fast rule-based logic) from enrichment (expensive AI inference), with patterns that survive production load.

I had a conversation last week with an engineering director at a fintech company. They were building account takeover detection into their payment platform. The requirement was simple: detect suspicious activity within thirty seconds. Their Kafka cluster was processing transactions at one thousand events per second with consumer lag under two hundred milliseconds. The infrastructure was bulletproof.

Then they added the AI detection layer, and everything fell apart.

The problem isn't what you think it is. It's not that streaming is bad or AI is slow. It's that nobody had an honest conversation about what "real-time" actually means when you add a language model inference layer on top of events. Most streaming engineers carry a mental model inherited from the Kafka era—predictable latency, bounded queues, backpressure. Language models shatter all three assumptions. They have variable latency measured in hundreds of milliseconds to seconds. They destroy backpressure mechanisms. They make state management critical and expensive.

Let me walk you through the architectural problem that breaks conventional streaming wisdom and the patterns that actually survive contact with production.

## The Latency Budget: The Fundamental Mismatch

When you build a traditional Kafka consumer, your mental model is precise. An event arrives at timestamp T. You parse the event (2ms). You check a cache for user context (3ms). You write a result to a local store (2ms). Total latency: seven milliseconds. At one thousand events per second, you need seven coroutines. You have a hundred. Easy. You're massively under-subscribed.

Now add AI inference to that pipeline. The same event arrives at timestamp T. You parse it (2ms). You assemble context by hitting Redis for the user's last twenty transactions (3ms with typical p50 latency). You call Claude Haiku for anomaly scoring—but here's the mismatch. The API call takes 180-200ms on average at p50. But p99 is 800ms. And if the inference API experiences load, p99 stretches to 2000ms. You process the response (1ms). You write to PostgreSQL (2ms). Total latency is now 188ms at p50, 808ms at p99.

Your SLA says you have thirty seconds. On paper, you have massive headroom. Operationally? You're bleeding room. Here's why.

At one thousand events per second, if each event takes 188ms at p50, you need 188 concurrent API calls in flight. That's manageable. But under load when p99 becomes p50—which happens during market volatility or attack scenarios—you need eight hundred concurrent calls. Most organizations configure their Kafka consumer with maybe fifty concurrent workers. Now you have a fundamental constraint: you can't process one thousand events per second through an API that has variable latency measured in hundreds of milliseconds.

Events start queueing in Kafka. Consumer lag begins climbing. At a thousand events per second with only fifty workers, and assuming average latency of 200ms, you're processing fifty workers × (1000/200) = two hundred fifty events per second. You have a backlog of seven hundred fifty events per second accumulating in the consumer queue. After ten seconds, you have seven thousand five hundred events sitting unprocessed. The critical path to anomaly detection is now seven and a half seconds plus the API call time. A fraud transaction gets detected at T+8 seconds, not T+0.2 seconds.

The real latency budget looks like this:

- Event ingestion and queueing: 50ms (Kafka batching)
- Context assembly (Redis lookup): 3-5ms p50, 20ms p99
- AI inference: 180-200ms p50, 800-2000ms p99
- Result processing: 1ms
- PostgreSQL write: 2ms
- **Total critical path: 236ms p50, 2870ms p99**

But that's per individual event. Under load, add consumer lag:

- Consumer backlog at 1000 events/sec with 50 workers processing at 200ms each: 7-10 seconds
- Total operational latency: 7.2 seconds + 0.236 seconds = **7.4 seconds p50 under load**
- **At p99: 7.2 seconds + 2.8 seconds = 10 seconds**

Your SLA is thirty seconds. Your actual latency under realistic load is ten seconds. That sounds fine. But it's not, because you don't get to process every event. You're dropping throughput. And every skipped transaction during peak fraud attack is a missed detection.

The mistake most teams make is thinking about SLA as the time budget. The real constraint is throughput × latency = queue size. If you're processing one thousand events per second and each takes two hundred milliseconds, you're consuming two hundred event-seconds of queue space every second. Your consumer needs to drain at least two hundred events per second just to break even. If your consumers can only process at one hundred fifty events per second due to API constraints, you're falling behind. Every second you fall further behind. Within ten seconds you have one thousand events queued. Within thirty seconds you have three thousand. Your SLA is now meaningless because you're not even processing the events that come in.

## Why Streaming + AI Breaks Backpressure

Traditional backpressure is supposed to be your friend in streaming systems. If a consumer can't keep up, it signals the producer to slow down. The whole system reaches equilibrium. Kafka's consumer groups implement this. Consumer lag grows. The producer (or alerting on producer-side latency) detects it and slows down ingestion. The system self-regulates.

But when you introduce an external AI dependency with unpredictable latency, backpressure breaks catastrophically.

Your Kafka topic accepts one thousand events per second. Your consumer group has a target of processing one thousand events per second, so it stays in equilibrium. But the consumer's actual throughput depends on the AI API. The Claude Haiku API has a quota of two hundred calls per second per org (this varies by plan, but assume this for now). Your consumer is configured with fifty workers. Each worker processes serially. They need one thousand events per second ÷ two hundred API quota = five events per API call. They batch them for efficiency. But here's the problem: the API quota is shared. Maybe another team is using Claude for their own pipeline. Maybe they're using three hundred calls per second. Suddenly your available quota drops to negative. You're hitting rate limits.

Now your consumer implements retry logic with exponential backoff. It tries to send an event. Gets rate-limited. Waits 100ms. Retries. Gets rate-limited again. Waits 200ms. Eventually backoff reaches thirty seconds. During those thirty seconds, that worker is doing nothing. Fifty workers, maybe five of them are in backoff. Now you're processing at one thousand × (45/50) = nine hundred events per second. You're falling behind by one hundred per second. Consumer lag grows.

But here's the critical failure mode: the backpressure signal never reaches upstream. Kafka's producer doesn't know the consumer is struggling. It keeps sending one thousand per second. The Kafka broker accepts them all. The consumer group lag climbs to ten thousand events. Eventually, the events are so old that you're detecting fraud for transactions that happened thirty seconds ago. The fraud is already settled. The money already moved. Your detection is useless.

This is why every production system I've seen at scale makes a fundamental architectural decision early: don't put AI in the critical synchronous path. Instead, decouple into two paths. The critical path uses rule-based logic. It's fast. It's deterministic. It doesn't have external dependencies. The secondary path uses AI asynchronously. It enriches data over time.

The pattern: an event arrives. You immediately evaluate it against rules. Is the IP geographically blocked? Is the account flagged? Is the transaction pattern known-bad? These checks take 10-15ms. They happen synchronously. The user gets a response in fifteen milliseconds. Meanwhile, the AI enrichment happens asynchronously in the background. The event gets queued for semantic analysis. That analysis might take one second. It might take five seconds. It doesn't matter because the user already got their answer.

## The Context Assembly Problem: State Management Disguised as AI

Here's where most teams stumble fundamentally. They think they're building an AI inference service. What they're actually building is a distributed state management system. This is a critical distinction, and missing it early destroys your architecture.

A fraud detection model needs context. A single event from Kafka—a transaction fact—is almost meaningless. The model needs:

- User's last ten transactions (amounts, merchants, times)
- Typical transaction amount and velocity (how much do they usually spend per hour, per day, per week)
- User's last three login locations (geohashes)
- Known devices (device fingerprints, OS, browser)
- Login velocity (how often do they log in, from where, at what time)
- Recent IP address changes
- Account age and reputation
- Historical fraud flags
- Payment method details

That's fifteen separate contexts. A single event from Kafka gives you maybe three: transaction amount, merchant, timestamp. The other twelve live in various systems. Some are in Redis. Some are in your user service (accessed via gRPC or HTTP). Some are in historical databases. Some are in a feature store. Some you compute on the fly (rolling averages, velocity calculations).

Assembling this context is expensive. For each event, you're making six to twelve external lookups. If each lookup takes 3-5ms, you're at 18-60ms just for context assembly, before you even call the AI API. And that's assuming everything hits and the latency is normal. If Redis is slow that day, you're at 100ms just for context assembly. If your user service is flaky, you're doing retry logic.

But here's the deeper problem: this is now a join operation. You're joining the event stream (hot, fast-changing) with slowly-changing dimensions (user profiles, historical patterns). This is the nightmare scenario for distributed systems. You need transactional semantics, but you're working across multiple systems with no transaction boundaries. You need strong consistency, but you're assembling from eventual-consistency systems.

The context assembly pattern that works at production scale looks like this: Kafka consumer pulls an event. The consumer does a bounded lookup in Redis for hot context. Redis is configured with a 100-millisecond timeout (much tighter than the default). If Redis responds within 100ms, great, you have fresh context. If not, you timeout and fall back to cached defaults. Maybe you have a local in-memory cache from the last check. Maybe you have defaults ("assume typical user"). You're trading freshness for reliability. Then you call the AI API with whatever context you could assemble. When the response comes back, you update Redis with new context. The next event for this user will hit a warmer cache.

The critical insight is this: Redis becomes critical infrastructure. Your fraud detection availability SLA is now also your Redis availability SLA. If Redis goes down, you have two unacceptable choices. You can process events without enrichment (predictions are garbage). Or you can block processing and let consumer lag grow (you have detection gaps). This is why production systems implement defensive patterns: Redis with read replicas for failover, local cache fallbacks, tiered lookups, circuit breakers on the Redis connection.

The cost implications are also significant. For one thousand events per second, you're making five thousand to twelve thousand Redis lookups per second. That's expensive on managed Redis services. ElastiCache for Redis in AWS costs roughly one to three dollars per GB per month. To handle five thousand requests per second with 1-2ms latency, you need significant instance sizes. You might need a cache:db-large instance (25GB) costing around $1000 per month, just to store the context for one thousand events per second across all your users.

This state management problem is why pure streaming architectures often fail for AI workloads. State is expensive. Fresh state is even more expensive. The system becomes bottlenecked on context assembly, not on inference.

## The Architecture Pattern: Async with Priorities

```
┌─────────────────────────────────────────────────────────────────────────┐
│                 HYBRID STREAMING + AI ARCHITECTURE                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌───────┐    ┌───────────────────────────────────────────────┐        │
│  │ Kafka │───▶│           CRITICAL PATH (8ms)                 │        │
│  │ Topic │    │  ┌──────────┐  ┌──────────┐  ┌────────────┐  │        │
│  │       │    │  │  Parse   │─▶│  Redis   │─▶│ Rule-Based │──│──▶ Response
│  │1K evt/│    │  │  Event   │  │  Context │  │   Score    │  │   (8ms)│
│  │  sec  │    │  │  (2ms)   │  │  (3ms)   │  │  (3ms)     │  │        │
│  └───┬───┘    │  └──────────┘  └──────────┘  └────────────┘  │        │
│      │        └───────────────────────────────────────────────┘        │
│      │                                                                  │
│      │        ┌───────────────────────────────────────────────┐        │
│      └───────▶│         ASYNC ENRICHMENT PATH (0.2-2.2s)      │        │
│               │  ┌──────────┐  ┌──────────┐  ┌────────────┐  │        │
│               │  │  Batch   │─▶│  Claude  │─▶│  Process   │  │        │
│               │  │  Queue   │  │  Haiku   │  │  Response  │  │        │
│               │  │(10 evts  │  │  (180-   │  │            │  │        │
│               │  │ or 2sec) │  │  300ms)  │  │  Update    │  │        │
│               │  └──────────┘  └──────────┘  │  Redis +   │  │        │
│               │                               │  Postgres  │  │        │
│               │                               └─────┬──────┘  │        │
│               └─────────────────────────────────────┼─────────┘        │
│                                                      │                  │
│                                           ┌──────────▼──────────┐      │
│                                           │  Score > 80?        │      │
│                                           │  YES → PagerDuty    │      │
│                                           │  NO  → Store only   │      │
│                                           └─────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────┘
```

The pattern that survives production looks like this in detail:

An event arrives in the Kafka consumer at timestamp T. The consumer immediately checks against rule-based scoring (takes 5ms). Queries Redis for hot context with a 100ms timeout (takes 3ms, falls back to defaults after timeout). Computes a fast risk score: 0-30 (low), 31-70 (medium), 71-100 (high). Immediately returns this to the client/system that triggered the transaction. Processing time so far: 8ms. Total latency to user: 8ms.

Now, in the background (asynchronously), the consumer queues this event for AI semantic scoring. It doesn't wait for the response. The event joins a batch queue. For cost efficiency, the consumer batches events: every 10 events or every 2 seconds, whichever comes first. So this event waits between 0-2 seconds in a batch queue.

When the batch is ready, the consumer sends it to Claude Haiku with the assembled context. The API call takes 180-300ms (accepts the batch, returns structured JSON with risk_score: 0-100, anomaly_type, confidence, reasoning for each event). The consumer processes responses asynchronously. If an event scores >80, it triggers an alert (topic to PagerDuty). All scores are persisted to PostgreSQL for offline analysis.

Redis is updated with the new risk profile for this user. The next event benefits from this enrichment.

The latency profile is completely different:
- User-facing latency: 8ms (rule-based)
- AI enrichment latency: 0-2 seconds batch wait + 200ms API call = 0.2-2.2 seconds
- Alert latency: <2.5 seconds from event

This hybrid approach works because it separates concerns. Rule-based catches obvious cases immediately. AI enriches and improves confidence over time. The user experience is fast. The backend improves over time.

But micro-batching introduces complexity. You need to handle partial failures. What if one event in a batch of ten causes the API to fail? You implement exponential backoff with jitter. You implement circuit breakers: if three API calls fail in a row, stop sending requests for sixty seconds. You implement fallback: if the AI call fails, you've already returned a rule-based score to the user, so you just don't update the enrichment. The system degrades gracefully.

## The Backpressure and Batch Size Problem

Micro-batching reintroduces a form of backpressure, but it's adaptive. If your consumer lag climbs above some threshold (say five thousand events), the consumer increases batch size: instead of batching every 2 seconds, batch every 5 seconds. Instead of batching 10 events, batch 50 events. This reduces API call frequency and total cost. The tradeoff is latency: you wait longer for enrichment. But you survive the load spike.

If lag climbs even higher, you implement selective skipping: only send 50% of events to AI validation. Sample the rest. This further reduces cost and keeps the system from drowning.

The consumer tracks several metrics: consumer lag (how many events behind Kafka are we), API call latency (p50, p95, p99), API failure rate (how often does the request fail), batch queue size (how many events waiting to be batched), and total cost (API calls per second × model cost).

The system is self-regulating. As load increases, batch size increases, cost per event decreases, latency increases slightly. The system stays within bounds.

## When Batch Is Actually the Right Answer

I've watched teams build streaming systems under intense pressure to be "real-time." Everyone says we need real-time AI detection. Then six months later, the product team casually mentions, "Actually, we can do this analysis overnight. We just need the detection UI to refresh every five minutes."

This is the honest conversation that needs to happen before you build. Real-time means different things. For account takeover, you need sub-second response. For bot activity detection, you can do batch every 5 minutes. For unusual payment patterns, batch daily works. For churn prediction, batch hourly.

The pattern that works at scale is: streaming for detection (binary: block or allow), batch for analysis (probabilistic: 0-100 confidence). Stream catches obvious attacks. Batch finds subtle patterns.

The honest conversation: you ask product, "How quickly do we need to act?" Product says "real-time, as fast as possible." You ask, "What does act mean? Block the transaction? Show a warning? Log for review?" Product says "show a warning." You ask, "How confident do we need to be?" Product says "just flag anything unusual."

This changes everything. You don't need an AI model in the hot path. You need a batch model that runs every hour, looking at the last hour of transactions, finding pattern anomalies. You need a streaming rule-based detector that flags obvious cases. You need a UI that refreshes every few minutes. Cost drops from $30K/month to $3K/month. Latency is acceptable (warnings appear within minutes). Everyone's happy.

## Monitoring and Observability

Three separate dashboards, each telling you something about system health:

**Consumer health:** Consumer lag (should be <100 events or <5 seconds), commit rate (stable indicates healthy processing), exception count in context assembly (spikes indicate Redis or service problems), batch queue size (indicates backpressure).

**Inference performance:** API latency percentiles (p50, p95, p99 tracked separately), queue depth (events waiting for inference), timeout rate (% of calls that timeout and fallback), fallback rate (% of events using default context), cost per event, cost per hour.

**Economics:** Daily cost against budget, cost by model tier (Haiku vs Sonnet), cost anomalies (sudden spikes), cost per outcome (cost per fraud detection). This dashboard prevents the surprise $47K bill.

## Cloud Architecture: AWS vs GCP

**AWS Implementation:**

**MSK (Managed Streaming for Apache Kafka)** – The broker layer that stores and distributes events across your consumer group.
- Configuration: Standard provisioned cluster with auto-scaling. 3-5 broker nodes using msk.t3.small instances.
- Per-unit pricing: msk.t3.small = $0.189/hour per broker = ~$136/broker/month
- Calculation: 4 brokers × $136 = $544/month for brokers
- Storage: $1/GB-month. At 1000 events/second with 1KB payload, you ingest 86GB/day. With 7-day retention = 600GB total. Storage cost: $600/month
- **Total MSK monthly cost: $1,144**

**ECS Fargate (consumer service)** – Runs the Kafka consumer code that assembles context, batches, and calls the AI API.
- Configuration: 1 vCPU + 2GB RAM per task. You need 5-10 tasks to handle 1000 events/sec with concurrent API calls.
- Per-unit pricing: $0.04048/vCPU-hour + $0.004445/GB-hour
- vCPU cost: 8 tasks × 1 vCPU × 730 hours/month × $0.04048 = $237/month
- Memory cost: 8 tasks × 2GB × 730 hours/month × $0.004445 = $52/month
- **Total Fargate monthly cost: $289**

**ElastiCache Redis** – Stores hot context: user profiles, last N transactions, velocity metrics, device fingerprints. Critical for reducing external lookups.
- Configuration: db.r6g.xlarge (32GB) for handling 5000-10000 requests/second with sub-5ms latency
- Per-unit pricing: $0.485/hour on-demand
- Calculation: $0.485 × 730 hours = $354/month (base)
- Multi-AZ for high availability adds 100%: $354 × 2 = $708/month
- **Total Redis monthly cost: $708**

**RDS PostgreSQL** – Stores all fraud scores, enrichment results, audit logs, feature history for offline analysis.
- Configuration: db.t4g.medium (1 vCPU, 4GB RAM) for production workload
- Per-unit pricing: $0.15/hour
- Instance cost: $0.15 × 730 = $110/month
- Storage: $0.20/GB-month. 500GB of historical data = $100/month
- Backup storage (7-day retention): ~$50/month
- **Total RDS monthly cost: $260**

**Direct Anthropic API** – Batch inference calling Claude Haiku on micro-batches of 10-50 events.
- Pricing: $0.80 per million input tokens
- Calculation: 1000 events/sec × 500 tokens/event × 86,400 sec/day = 43.2B tokens/day
- Daily cost: 43.2B × ($0.80 / 1M) = $34.56/day
- **Total API monthly cost: $1,037**

**SNS for alerts** – Notifies PagerDuty, Slack, and internal dashboards when high-risk transactions occur.
- Pricing: $0.50 per million notifications
- Calculation: Assuming 5-15 alerts/sec = 432K-1.3M alerts/day = 13-40M/month
- **Total SNS monthly cost: $7-20**

**CloudWatch** – Custom metrics for monitoring latency, throughput, failures, and costs.
- Pricing: $0.30/metric/month (custom metrics; dashboards are free)
- Count: Consumer lag, API p50/p95/p99, failure rates, batch queue depth, token spend, cost anomalies = 15-20 metrics
- **Total CloudWatch monthly cost: $5-6**

**Total AWS:** $544 + $289 + $708 + $260 + $1,037 + $14 + $6 = **$2,858/month for infrastructure + Anthropic API**

**Trade-Offs within AWS:**

**MSK vs Kinesis Data Streams**
- Kinesis Data Streams is simpler (no broker management) but more expensive
- Kinesis pricing: $0.015/shard-hour = $10.80/shard/month
- For 1000 events/sec, you need ~2 shards = $21.60/month (dramatically cheaper than MSK's $1,144)
- BUT Kinesis has 1MB/sec throughput per shard. At 1KB/event × 1000 events/sec = 1MB/sec, you're hitting the limit
- If your fraud events are larger (2-5KB for rich context), you need 3-5 shards = $32-54/month
- MSK advantage: precise control over partitioning (critical for per-user ordering in fraud detection). You can guarantee all events for user X go to partition Y, ensuring strict ordering
- Kinesis advantage: Kinesis doesn't offer per-user partitioning control; it hashes keys randomly across shards
- **Verdict: MSK wins if you need strict per-user fraud ordering (recommended). Kinesis wins if you're building a non-critical enrichment pipeline or can tolerate eventual consistency**

**Fargate vs EC2 for consumers**
- Fargate: $0.04048/vCPU-hour (no cluster management required)
- EC2 t3.large (2 vCPU, 8GB): $0.0832/hour = $0.0416/vCPU-hour—slightly cheaper, requires managing ECS cluster, Auto Scaling Group, patching
- At 8 consumer tasks running 24/7, Fargate costs $237/month. EC2 would cost ~$200/month
- EC2 requires: purchasing reserved instances upfront for bigger discounts, managing Docker registry, cluster orchestration, and security patching
- Fargate advantage: Pay-per-task, integrates with Fargate Spot (30-70% cheaper), simpler operations for small teams
- **Verdict: Fargate wins for simplicity unless you have >50 containers running. For a single consumer cluster, the operational overhead of EC2 outweighs the $37 savings**

**Direct Anthropic API vs Bedrock for streaming**
- Direct Anthropic API: ~10ms overhead, no rate limiting proxy overhead, direct account quotas
- AWS Bedrock: $0.003 per input token (Haiku pricing through Bedrock) plus managed rate limiting, but adds ~50-100ms proxy latency
- At 1000 events/sec, 500 tokens/event, that's 500K tokens/sec
- Direct API cost: (500K tokens × $0.80 / 1M) × 86,400 sec/day = $34.56/day = $1,037/month
- Bedrock cost: (500K tokens × $0.003) × 86,400 sec/day = $129.60/day = $3,888/month (3.75× more expensive!)
- Bedrock advantage: Managed rate limiting, integrated with AWS console, easier for multi-region failover if you're already on Bedrock
- **Verdict: Direct API wins for cost and latency. Only use Bedrock if you need multi-region failover or are building for non-time-sensitive workloads**

**GCP Implementation:**

**Pub/Sub** – Event stream. Replaces Kafka with Google's managed topic/subscription model.
- Configuration: Topic with default 7-day retention, multiple subscriptions for parallel consumers
- Pricing: $0.20/million messages ingested + $0.40/million messages delivered (per subscription)
- Ingestion: 1000 events/sec × 86,400 sec/day = 86.4M events/day × 30 days = 2.59B/month
- Cost: 2.59B × ($0.20 / 1B) = $0.52/month (ingestion is negligible)
- Delivery (1 subscription): 2.59B × ($0.40 / 1B) = $1.04/month (also minimal)
- **Total Pub/Sub monthly cost: $1.56**

**Cloud Run** – Managed container service that runs the consumer code. Bills only for execution time.
- Configuration: 1 vCPU, 2GB memory. You set 8 instances with concurrency=10 (up to 80 concurrent requests)
- Pricing: $0.00002400/vCPU-second + $0.0000025/GB-second
- Calculation (assuming 80% utilization across 8 instances): 8 instances × 1 vCPU × 0.8 × 86,400 sec/day × 30 days = 16.6M vCPU-seconds
- vCPU cost: 16.6M × $0.00002400 = $398/month
- Memory cost: 16.6M × 2GB × $0.0000025 = $83/month
- **Total Cloud Run monthly cost: $481**

**Memorystore for Redis** – Managed Redis for context caching. Same role as ElastiCache.
- Configuration: standard.m4.large (32GB) with 99.9% availability SLA
- Per-unit pricing: $0.45/hour on-demand
- Calculation: $0.45 × 730 hours = $329/month
- High availability (replicas): +$329 = $658/month
- **Total Memorystore monthly cost: $658**

**Cloud SQL for PostgreSQL** – Managed PostgreSQL database for score storage.
- Configuration: db-g1-small (1 vCPU, 3.75GB) for continuous operation
- Per-unit pricing: $0.078/hour
- Instance cost: $0.078 × 730 = $57/month
- High availability replica: +$57 = $114/month
- Storage: $0.17/GB-month. 500GB = $85/month
- **Total Cloud SQL monthly cost: $256**

**Anthropic API** – Same as AWS, $1,037/month for Haiku inference

**Cloud Logging & Monitoring** – Included in GCP pricing. No per-metric fees.
- Cloud Logs ingestion: First 50GB/month free, then $0.50/GB. Streaming logs are ~2-5GB/month (well under free tier)
- **Total Monitoring cost: $0 (free tier covers it)**

**Total GCP:** $1.56 + $481 + $658 + $256 + $1,037 = **$2,433/month for infrastructure + Anthropic API**

**Trade-Offs within GCP:**

**Pub/Sub vs Cloud Kafka (Confluent Cloud on GCP) vs self-managed MSK**
- Pub/Sub: $1.56/month (extremely cheap), but no partition-level ordering guarantees. Messages are delivered at-least-once, but a single user's events might arrive out of order across consumers
- Cloud Kafka (Confluent): ~$500-1000/month for comparable throughput, full Kafka semantics including per-partition ordering
- Self-managed MSK on GCP (via Compute Engine): Similar to AWS cost ($1,144)
- Pub/Sub advantage: Serverless, zero ops, integrates natively with Cloud Run and other GCP services
- Pub/Sub disadvantage: No per-user ordering. If user fraud detection depends on transaction ordering, Pub/Sub might drop detections
- **Verdict: Pub/Sub if ordering doesn't matter (enrichment pipelines, non-critical alerts). Cloud Kafka or self-managed MSK if strict ordering is required for fraud detection**

**Cloud Run vs GKE vs Cloud Functions for consumers**
- Cloud Run (container, billed per execution): $481/month at 80% utilization
- GKE Standard (managed Kubernetes): 3-node cluster (2 user nodes) = $120/month cluster fee + ~$150/month compute for nodes = $270/month + operational overhead
- Cloud Functions (serverless, billed per invocation): Starts cheap (~$50/month) but can explode with high concurrency. For 1000 events/sec, you'd need 1000 concurrent functions, which costs ~$3000-5000/month
- Cloud Run advantage: Pay for actual execution time (not reserved capacity), scales from zero, better latency than Functions
- **Verdict: Cloud Run wins for streaming consumers. GKE is overkill for a single consumer. Cloud Functions is wrong for this workload**

**Pub/Sub with Cloud Functions vs Cloud Run vs Dataflow for streaming**
- Pub/Sub + Cloud Functions: Cheapest entry (Functions free tier covers initial load), cold start latency (500-2000ms per function invocation) unacceptable for <200ms SLA
- Cloud Run with Pub/Sub: Recommended pattern. Warm containers, predictable latency, scales elastically
- Cloud Dataflow (Apache Beam): Overkill for this use case. Better for large-scale transformations across terabytes. Minimum cost ~$400/month even at low volume
- **Verdict: Cloud Run is the sweet spot—warm instances, sub-200ms latency, good cost efficiency, scales with load**

## When Streaming + AI Is Overkill

Before you build a real-time streaming system with AI inference, ask yourself these hard questions. Most teams overengineer this problem.

**Your detection SLA is actually >5 minutes**
Real-time sounds impressive. But if your product can accept a 5-minute detection delay, batch every 5 minutes instead of streaming. Your cost drops 80%.

Example: You're detecting bot activity on user signups. Batch every 5 minutes: run inference on all signups from the last 5 minutes (say 500 events), send one batch to Claude, get results, write to PostgreSQL. Cost: one API call every 5 minutes. At 500 tokens per event, that's 250K tokens × 12 batches/hour × $0.80/1M = $0.002/hour = ~$15/month in API costs.

Compare to streaming: 1000 events/sec, micro-batch every 2 seconds, 1 API call every 2 seconds = 43,200 calls/day. Cost: $1,000+/month just in inference.

If your SLA is "detect within 5 minutes," batch. If it's "detect within 10 seconds," stream. Have this conversation with product before building.

**Your event volume is <100 events per second**
Stop. You don't need Kafka. You don't need a managed message queue. You don't need a consumer group. Use a simple Lambda function triggered by CloudWatch Events every 30 seconds, pull the last 30 seconds of events from your application database, enrich them, write results back. Total infrastructure cost: $5-10/month.

Real numbers: 100 events/sec × 30 seconds = 3,000 events per Lambda invocation. Lambda pricing: 3M invocations/month × $0.0000002 = $0.60/month. Database reads: negligible. API calls: 6 calls/minute × 60 min × 24 hours × 30 days = 259K calls/month. Assuming 500 tokens/call, that's 130M tokens = $104/month. Total: ~$110/month.

Compare to: MSK ($1,144) + Fargate ($289) + Redis ($708) + API ($1,037) = $3,178/month.

If your volume is low, use batch + Lambda. Streaming is overhead you don't need.

**Your AI enrichment isn't time-sensitive**
You have two classes of processing: detection (must be fast, binary output) and analysis (can be slow, probabilistic output).

If all your AI work falls into the analysis bucket—computing confidence scores, trend detection, pattern anomalies for offline reporting—decouple completely. Stream the raw events to a data lake (S3 or GCS). Run batch enrichment nightly. Cost: storage ($10-20/month for S3) + one nightly batch job ($5/month) = $25/month.

Don't put the AI in the critical path if the AI results don't affect real-time decisions.

**Your rule-based detection already catches >90% of cases**
This is the honest conversation with product. You say: "We can add AI enrichment for the remaining 10% of cases, but it's expensive and adds latency. Or we can optimize the rule engine to catch 95% with no latency penalty."

Most of the time, optimizing rules is the right call. Because the rule engine is deterministic. It's fast. It's cheap. Adding AI for diminishing returns is a trap.

Example fraud detection rule engine:
- Rule 1: If transaction amount > $10,000, flag. (Catches 15% of fraud)
- Rule 2: If user's last login from different country in last 24 hours, flag. (Catches 20%)
- Rule 3: If transaction velocity > 3 per hour, flag. (Catches 25%)
- Rule 4: If merchant is in high-risk category + unusual amount, flag. (Catches 15%)
- Total: ~75% of fraud caught with rules.

Now you consider AI. AI might catch the remaining 25%, but only with high confidence. So AI adds maybe 8-10% catch rate on top of rules (because it's imperfect, and you set a high threshold to avoid false positives).

Rule optimization:
- Tweak Rule 2: Flag if login from new country + amount > usual
- Add Rule 5: If multiple failed login attempts in last hour + transaction, flag
- Tune thresholds based on false positive rate
- New total: 92% catch rate.

Cost: You optimized rules (free), added one more rule (free). Latency: 10-15ms (all local checks).

vs. AI approach:
- Keep rules
- Add AI enrichment on all events
- Catch 92-95% with AI + rules
- Cost: $3K+/month in infrastructure
- Latency: 200ms+ (API overhead)

The rule-optimized approach wins. Optimize rules first. Add AI only if rules plateau.

## Data Engineering Fundamentals: Streaming + AI Patterns

**Exactly-Once Semantics**

The hybrid architecture achieves a nuanced form of exactly-once guarantees by separating concerns. The critical path uses rule-based scoring—deterministic and idempotent. The async enrichment path is eventually consistent. If the Claude API call fails, the rule-based score still exists. The enrichment is additive, not destructive. This means: rule-based results are guaranteed. AI enrichment is best-effort. Downstream consumers must handle both states (enriched vs. not-yet-enriched). The system never loses detection capability, but may temporarily lack confidence scores until enrichment completes.

**Idempotency in Streaming**

The async batch processor uses (event_id, enrichment_version) as the composite key in PostgreSQL. If a batch is retried due to partial failure, the UPSERT ensures no duplicate enrichments. Redis context updates are naturally idempotent (SET operations overwrite). This pattern allows for aggressive retry logic without fear of corrupting state. A failed batch of 10 events can be retried 5 times, and the 10th event will still have exactly one enrichment record. No accumulation. No double-counting. The version field (incremented on each enrichment iteration) prevents stale enrichments from overwriting fresh ones.

**State Management as Data Engineering Problem**

Redis isn't just a cache—it's a stateful join between the event stream (hot, fast-changing) and user dimensions (slow, historical). This is the streaming equivalent of a star schema join. The context assembly pattern (lookup 6-12 fields from multiple sources) is essentially a denormalized dimension table maintained in real-time. Every Redis lookup is a dimension table access. Every Redis write is a dimension table update. The latency of the fraud detection system is directly tied to how efficiently you maintain these dimensions. A stale user_profile dimension causes incorrect scoring. A missing transaction_velocity dimension causes fallback to defaults, degrading accuracy.

**Backpressure as Data Quality Signal**

When consumer lag climbs, it's not just a performance problem—it's a data quality signal. Events processed with 10-second lag have stale context (the user might have done 10 more transactions). The enrichment is based on outdated state. A user's risk score computed with transactions from 10 seconds ago is qualitatively different from the same score computed with current data. Monitor lag not just for performance, but for semantic accuracy of enrichments. When lag exceeds a threshold (say 5 seconds), consider this a quality event: enrich less frequently, use wider batches, accept stale context. Trade freshness for processing guarantee.

**Service-Level Agreements**

Three distinct SLAs exist, each with different implications:

1. **Critical path: <50ms p99** – Rule-based scoring latency. This is the user-facing SLA. If this breaches, users experience slow transactions. The SLA is tight because nothing should add latency here. Redis latency issues, GC pauses, CPU contention—all cause breaches.

2. **Enrichment: <5 seconds for 95% of events** – AI scoring latency. This is asynchronous, so breaches don't affect users directly. But breaches indicate you're falling behind on context assembly. If enrichment is delayed, downstream decisions (alert routing, model retraining) are delayed.

3. **Alert: <10 seconds from event to PagerDuty notification** – Critical path (8ms) + enrichment decision + alert routing (<2s). If an event scores >80, how long until the on-call engineer gets paged? This SLA determines whether your alerts are actionable or historical.

Each SLA has different failure modes and requires different monitoring. Critical path failures are detected via p99 latency spikes. Enrichment failures are detected via lag growth. Alert failures are detected via end-to-end tracing.

**Cost as a Runtime Constraint**

Unlike batch pipelines where cost is fixed, streaming cost scales with throughput × latency. During peak hours (2× normal volume), cost doubles. During incidents (API latency spikes), cost per event increases because retries consume tokens. Budget must account for peak, not average.

For example, normal operation: 1000 events/sec × 500 tokens/event × $0.80/1M tokens = $34.56/day. During peak (2× volume): $69.12/day. During an incident (API has 2× latency, so 2× retries): $138/day. Over a month, peak hours might happen 5 days/month, incidents 2 days/month. Average daily cost is not ($34.56 × 23 days + $69.12 × 5 days + $138 × 2 days) / 30 = $50/day, which is $1,500/month, not the $1,037/month budgeted assuming constant load.

Set aside 20% budget headroom for peaks and incidents, or implement cost guardrails (circuit breakers that reduce sample rate if daily spend exceeds threshold).

## Skills You've Developed

By building streaming systems with AI, you've learned latency budgeting. Every millisecond matters. External APIs destroy your assumptions. State management becomes the bottleneck. A single event means nothing. You need context. That context lives somewhere, and getting it fast is expensive. Hybrid architectures are necessary. Pure streaming doesn't work when inference is expensive. You need both.

Cost becomes a first-class concern. You know the cost implications before you build. Honest product conversations are essential. You can separate critical path from supporting operations.

## What's Next

You've built real-time streams with AI. You've battled latency, cost, and state management. Now you're shipping data to your warehouse with a new problem: how do you trust it? Two hundred data sources. Great Expectations catches sixty percent. Semantic problems slip through.

**Next article: "The Semantic Data Quality Layer Your Warehouse Is Missing"** – where we build the validation architecture that catches what rule-based tools miss, and how to design human review workflows that actually work.

---

## Code & Resources

**GitHub Repository:** [github.com/jay-jain-10/de-in-ai-series](https://github.com/jay-jain-10/de-in-ai-series)

**What's in the repo:**
- `articles/` — All 8 articles in this series as markdown files, including architecture diagrams, cost breakdowns, and trade-off analyses
- Each article is self-contained with AWS/GCP service recommendations, DE fundamentals sections, and worked examples you can adapt to your own pipelines

**How to use this series:** Read the articles in order (each builds on concepts from the previous one), then use the architecture diagrams and cost models as starting points for your own AI pipeline designs. Fork the repo and customize the patterns for your specific cloud environment.

*Part 5 of "Data Engineering in the Age of AI"*
