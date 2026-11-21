"""Cost attribution per team / pipeline / task, with Kafka cost-event streaming."""

import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

from ..contracts import CallerMetadata, CostEvent, TaskType
from ..messaging import Publisher


class CostTracker:
    def __init__(self, pricing: dict, publisher: Optional[Publisher] = None,
                 topic: str = "gateway.cost_events",
                 clock: Callable[[], float] = time.time):
        self.pricing, self.publisher, self.topic, self._clock = pricing, publisher, topic, clock
        self.events: list[CostEvent] = []

    def price(self, model: str, input_tokens: int, output_tokens: int) -> float:
        p = self.pricing[model]
        return (input_tokens / 1_000_000) * p["input"] + (output_tokens / 1_000_000) * p["output"]

    def record(self, caller: CallerMetadata, task: TaskType, model: str, input_tokens: int,
               output_tokens: int, cached: bool, prompt_version: str = "",
               called_at: Optional[datetime] = None) -> CostEvent:
        """Cache hits are logged too (cost 0) so hit-rate is visible per team."""
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        cost = 0.0 if cached else self.price(model, input_tokens, output_tokens)
        event = CostEvent(
            event_id=uuid.uuid4().hex, called_at=called_at or now, logged_at=now,
            team=caller.team, pipeline=caller.pipeline, task_type=caller.task_type,
            task=task.value, model=model,
            input_tokens=0 if cached else input_tokens,
            output_tokens=0 if cached else output_tokens,
            cost_usd=round(cost, 8), cached=cached, prompt_version=prompt_version)
        self.events.append(event)
        if self.publisher:
            self.publisher.publish(self.topic, event.team, event.model_dump(mode="json"))
        return event

    def total(self, team: Optional[str] = None) -> float:
        return sum(e.cost_usd for e in self.events if team in (None, e.team))

    def report(self) -> dict:
        """Month-end style report: spend and share of total per team, with pipeline breakdown."""
        by_team: dict[str, float] = defaultdict(float)
        by_pipeline: dict[str, float] = defaultdict(float)
        by_task: dict[str, float] = defaultdict(float)
        calls: dict[str, int] = defaultdict(int)
        hits: dict[str, int] = defaultdict(int)
        for e in self.events:
            by_team[e.team] += e.cost_usd
            by_pipeline[f"{e.team}/{e.pipeline}"] += e.cost_usd
            by_task[f"{e.team}/{e.pipeline}/{e.task_type}"] += e.cost_usd
            calls[e.team] += 1
            hits[e.team] += int(e.cached)
        total = sum(by_team.values())
        return {
            "total_usd": round(total, 6),
            "by_team": {t: {"usd": round(v, 6), "share": round(v / total, 4) if total else 0.0,
                            "calls": calls[t], "cache_hit_rate": round(hits[t] / calls[t], 4)}
                        for t, v in by_team.items()},
            "by_pipeline": {k: round(v, 6) for k, v in by_pipeline.items()},
            "by_task_type": {k: round(v, 6) for k, v in by_task.items()},
        }

    def budget_alerts(self, budgets: dict[str, float], threshold: float = 0.8) -> list[dict]:
        alerts = []
        for team, budget in budgets.items():
            if budget <= 0:
                continue
            spent = self.total(team)
            if spent >= budget * threshold:
                alerts.append({"team": team, "spent_usd": round(spent, 6), "budget_usd": budget,
                               "utilization": round(spent / budget, 4),
                               "severity": "exceeded" if spent >= budget else "warning"})
        return alerts
