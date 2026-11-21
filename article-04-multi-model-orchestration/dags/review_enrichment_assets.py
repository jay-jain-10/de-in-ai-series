"""Dagster asset graph with a model-aware I/O manager (Article 4).

    review_text
      -> sentiment_classifications
      -> aspect_classifications        (only reviews with sentiment == negative)
      -> toxicity_classifications      (only reviews whose aspects flagged unsafe language)
      -> final_review_enrichment       (aggregation + lineage)

Per-asset policy (from config/router.yaml, mirrored here as RetryPolicy / pool / metadata):
    sentiment   timeout 1s  retry 2x  max_concurrency 1000
    aspect      timeout 3s  retry 1x  max_concurrency 100
    toxicity    timeout 5s  retry 1x  max_concurrency 50
    aggregation timeout 2s  retry 3x

The per-call timeout is enforced in ModelGateway (Anthropic client timeout), not as a
Dagster op timeout, because an op processes a whole batch. The I/O manager routes outputs
by data: fallbacks -> *_fallback table; toxicity confidence < 0.7 -> needs_human_review.
Scheduled daily at 2am.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from dagster import (
    AssetKey, AssetSelection, Definitions, IOManager, InputContext, OutputContext,
    ResourceDefinition, RetryPolicy, ScheduleDefinition, asset, define_asset_job,
)

from src.lineage import build_lineage
from src.gateway import ModelGateway
from src.results_store import InMemoryResultStore, to_row
from src.router import ModelRouter
from src.schemas import TaskResult

TASK_ASSETS = {
    "sentiment_classifications": "sentiment",
    "aspect_classifications": "aspect_extraction",
    "toxicity_classifications": "toxicity_detection",
}


@dataclass
class PipelineResources:
    gateway: ModelGateway
    router: ModelRouter
    load_reviews: Callable[[], list]   # returns [{"review_id", "text"}]

    def policy(self, task: str):
        return self.gateway.config.tasks[task]


def run_concurrently(fn, items: list, max_concurrency: int, worker_cap: int = 32) -> list:
    """Bounded parallel map preserving order; width = min(max_concurrency, worker_cap, n)."""
    if not items:
        return []
    workers = max(1, min(max_concurrency, worker_cap, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


class ReviewResultsIOManager(IOManager):
    """Persists task outputs through the idempotent store and routes rows by their data."""

    def __init__(self, store: InMemoryResultStore):
        self.store = store
        self._mem: dict = {}

    def handle_output(self, context: OutputContext, obj) -> None:
        key = "/".join(context.asset_key.path)
        self._mem[key] = obj
        if key in TASK_ASSETS and obj:
            stats = self.store.merge([to_row(r) for r in obj])
            context.add_output_metadata({
                "rows_inserted": stats["inserted"], "rows_updated": stats["updated"],
                "fallbacks": sum(1 for r in obj if r.fallback),
            })

    def load_input(self, context: InputContext):
        return self._mem[("/".join(context.upstream_output.asset_key.path))]


@asset(group_name="reviews", required_resource_keys={"pipeline"})
def review_text(context) -> list:
    reviews = context.resources.pipeline.load_reviews()
    context.add_output_metadata({"review_count": len(reviews)})
    return reviews


@asset(group_name="reviews", required_resource_keys={"pipeline"},
       retry_policy=RetryPolicy(max_retries=2), pool="sentiment")
def sentiment_classifications(context, review_text: list) -> list:
    p: PipelineResources = context.resources.pipeline

    def work(review):
        d = p.router.decide("sentiment", review["text"])
        return p.gateway.call("sentiment", d.model, review["review_id"], review["text"],
                              decision=d.reason)
    return run_concurrently(work, review_text, p.policy("sentiment").max_concurrency)


@asset(group_name="reviews", required_resource_keys={"pipeline"},
       retry_policy=RetryPolicy(max_retries=1), pool="aspect_extraction")
def aspect_classifications(context, review_text: list, sentiment_classifications: list) -> list:
    p: PipelineResources = context.resources.pipeline
    text_by_id = {r["review_id"]: r["text"] for r in review_text}
    eligible = [s for s in sentiment_classifications
                if not s.fallback and s.result.get("sentiment") == "negative"]
    context.add_output_metadata({"eligible": len(eligible), "skipped": len(sentiment_classifications) - len(eligible)})

    def work(s: TaskResult):
        text = text_by_id[s.review_id]
        d = p.router.decide("aspect_extraction", text)
        return p.gateway.call(
            "aspect_extraction", d.model, s.review_id, text,
            context={"sentiment": s.result["sentiment"], "confidence": s.confidence},
            decision=d.reason, parent_task="sentiment")
    return run_concurrently(work, eligible, p.policy("aspect_extraction").max_concurrency)


@asset(group_name="reviews", required_resource_keys={"pipeline"},
       retry_policy=RetryPolicy(max_retries=1), pool="toxicity_detection")
def toxicity_classifications(context, review_text: list, aspect_classifications: list) -> list:
    p: PipelineResources = context.resources.pipeline
    text_by_id = {r["review_id"]: r["text"] for r in review_text}
    eligible = [a for a in aspect_classifications
                if not a.fallback and a.result.get("unsafe_language")]
    context.add_output_metadata({"eligible": len(eligible)})

    def work(a: TaskResult):
        text = text_by_id[a.review_id]
        d = p.router.decide("toxicity_detection", text)
        # unsafe language was flagged upstream, so always use the safety-critical model
        model = p.gateway.config.tasks["toxicity_detection"].fallback_model
        return p.gateway.call("toxicity_detection", model, a.review_id, text,
                              decision=f"aspect_unsafe_language; {d.reason}",
                              parent_task="aspect_extraction")
    return run_concurrently(work, eligible, p.policy("toxicity_detection").max_concurrency)


@asset(group_name="reviews", required_resource_keys={"pipeline"},
       retry_policy=RetryPolicy(max_retries=3))
def final_review_enrichment(
    context, review_text: list, sentiment_classifications: list,
    aspect_classifications: list, toxicity_classifications: list,
) -> list:
    p: PipelineResources = context.resources.pipeline
    threshold = p.gateway.config.human_review_confidence
    asp = {r.review_id: r for r in aspect_classifications}
    tox = {r.review_id: r for r in toxicity_classifications}
    rows = []
    for s in sentiment_classifications:
        parts = [s] + [x for x in (asp.get(s.review_id), tox.get(s.review_id)) if x]
        t = tox.get(s.review_id)
        rows.append({
            "review_id": s.review_id,
            "sentiment": s.result.get("sentiment"),
            "aspects": asp[s.review_id].result.get("aspects") if s.review_id in asp else None,
            "toxic": t.result.get("toxic") if t else None,
            "total_cost_usd": round(sum(x.cost_usd for x in parts), 8),
            "any_fallback": any(x.fallback for x in parts),
            "needs_human_review": any(x.fallback for x in parts)
            or bool(t and t.confidence < threshold),
            "lineage": build_lineage(parts),
        })
    context.add_output_metadata({"rows": len(rows), "total_cost_usd": sum(r["total_cost_usd"] for r in rows)})
    return rows


ALL_ASSETS = [review_text, sentiment_classifications, aspect_classifications,
              toxicity_classifications, final_review_enrichment]

daily_job = define_asset_job("daily_review_enrichment", selection=AssetSelection.all())
daily_schedule = ScheduleDefinition(job=daily_job, cron_schedule="0 2 * * *")  # daily at 2am


def build_definitions(pipeline: PipelineResources, store: InMemoryResultStore) -> Definitions:
    return Definitions(
        assets=ALL_ASSETS,
        jobs=[daily_job],
        schedules=[daily_schedule],
        resources={
            "pipeline": ResourceDefinition.hardcoded_resource(pipeline),
            "io_manager": ReviewResultsIOManager(store),
        },
    )
