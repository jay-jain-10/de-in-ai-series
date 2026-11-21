"""Lineage across models: parent/child chain per review with model, version, cost."""

from .schemas import TaskResult


def build_lineage(results: list) -> list:
    """Flatten TaskResults into lineage rows (review_id, task, parent_task, model, version,
    cost, latency). parent_task is set by the orchestrator when a task depends on another."""
    return [
        {
            "review_id": r.review_id, "task": r.task, "parent_task": r.parent_task,
            "model": r.model, "model_version": r.model_version,
            "cost_usd": r.cost_usd, "latency_ms": r.latency_ms,
            "cache_hit": r.cache_hit, "fallback": r.fallback,
        }
        for r in results
    ]


def lineage_summary(results: list) -> dict:
    """Per-review roll-up: which tasks ran, total cost, whether aspect/Opus ran."""
    by_review: dict = {}
    for r in results:
        d = by_review.setdefault(r.review_id, {"tasks": [], "models": [], "total_cost": 0.0})
        d["tasks"].append(r.task)
        d["models"].append(r.model)
        d["total_cost"] += r.cost_usd
    for d in by_review.values():
        d["aspect_executed"] = "aspect_extraction" in d["tasks"]
        d["opus_executed"] = any(m == "opus" for m in d["models"])
    return by_review
