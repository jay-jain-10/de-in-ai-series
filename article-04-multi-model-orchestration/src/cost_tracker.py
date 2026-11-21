"""Cost-tracking middleware: every call logged with tokens, cost, latency, decision metadata."""

import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from .latency import percentile

logger = logging.getLogger("model_calls")  # ship to CloudWatch / Cloud Logging


@dataclass
class CallRecord:
    timestamp: str
    task: str
    model: str
    review_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    model_decision: str
    cache_hit: bool
    confidence: float
    fallback: bool = False
    model_version: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CostTracker:
    def __init__(self):
        self.records: list = []
        self._lock = threading.Lock()

    def record(self, rec: CallRecord) -> None:
        with self._lock:
            self.records.append(rec)
        logger.info(rec.to_json())

    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.records)

    def cost_for_review(self, review_id: str) -> float:
        return sum(r.cost_usd for r in self.records if r.review_id == review_id)

    def summary(self) -> dict:
        """Aggregate by task then model (the background job in the article)."""
        grouped = defaultdict(list)
        for r in self.records:
            grouped[(r.task, r.model)].append(r)
        out: dict = defaultdict(dict)
        for (task, model), recs in grouped.items():
            lat = [r.latency_ms for r in recs if not r.cache_hit]
            out[task][model] = {
                "calls": len(recs),
                "cost_usd": round(sum(r.cost_usd for r in recs), 6),
                "avg_cost_per_call": round(sum(r.cost_usd for r in recs) / len(recs), 6),
                "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else 0.0,
                "p95_latency_ms": round(percentile(lat, 95), 1) if lat else 0.0,
                "cache_hits": sum(1 for r in recs if r.cache_hit),
                "fallbacks": sum(1 for r in recs if r.fallback),
            }
        return dict(out)

    def model_share(self, task: str, model: str) -> float:
        task_recs = [r for r in self.records if r.task == task]
        if not task_recs:
            return 0.0
        return sum(1 for r in task_recs if r.model == model) / len(task_recs)

    def share_anomaly(self, task: str, model: str, expected: float, tolerance: float = 0.15) -> Optional[str]:
        """Detect e.g. Opus suddenly handling 60% of toxicity calls instead of 5%."""
        share = self.model_share(task, model)
        if abs(share - expected) > tolerance:
            return f"{task}/{model} share {share:.0%} vs expected {expected:.0%}"
        return None
