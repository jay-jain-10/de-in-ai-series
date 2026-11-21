"""Cost SLAs, enforced by the cost wrapper.

1. Max cost per record: $0.005
2. Max daily cost:      $200 per day
3. Variance threshold:  2x daily average triggers investigation
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from src.config import CostSLAConfig
from src.cost_events import CostEvent

logger = logging.getLogger(__name__)


class CostBudgetExceeded(RuntimeError):
    """Raised when a pipeline has exhausted its daily budget (throttle/pause)."""


@dataclass
class SLAViolation:
    kind: str          # per_record | daily | variance
    subject: str       # record_id or pipeline_name
    observed: float
    limit: float
    message: str


def check_record_sla(cost_usd: float, sla: CostSLAConfig = None) -> Optional[SLAViolation]:
    sla = sla or CostSLAConfig()
    if cost_usd > sla.max_cost_per_record_usd:
        return SLAViolation("per_record", "", cost_usd, sla.max_cost_per_record_usd,
                            f"record cost ${cost_usd:.6f} > ${sla.max_cost_per_record_usd}")
    return None


def check_daily_sla(daily_cost: float, sla: CostSLAConfig = None) -> Optional[SLAViolation]:
    sla = sla or CostSLAConfig()
    if daily_cost > sla.max_daily_cost_usd:
        return SLAViolation("daily", "", daily_cost, sla.max_daily_cost_usd,
                            f"daily cost ${daily_cost:.2f} > ${sla.max_daily_cost_usd}")
    return None


def check_variance(
    today_cost: float, history: Sequence[float], sla: CostSLAConfig = None
) -> Optional[SLAViolation]:
    """Alert when today's cost exceeds variance_threshold x average of history
    (article: normal $150/day, today $600/day -> alert)."""
    sla = sla or CostSLAConfig()
    if not history:
        return None
    avg = sum(history) / len(history)
    if avg <= 0:
        return None
    limit = avg * sla.variance_threshold
    if today_cost > limit:
        return SLAViolation("variance", "", today_cost, limit,
                            f"today ${today_cost:.2f} is {today_cost/avg:.1f}x the average ${avg:.2f}")
    return None


class SLAEnforcer:
    """Observes cost events; throttles a pipeline once its daily budget is spent."""

    def __init__(self, sla: CostSLAConfig = None):
        self.sla = sla or CostSLAConfig()
        self._daily: Dict[tuple, float] = defaultdict(float)     # (pipeline, date)
        self._record: Dict[tuple, float] = defaultdict(float)    # (pipeline, record_id)
        self.violations: List[SLAViolation] = []

    def daily_spend(self, pipeline_name: str, date: str) -> float:
        return self._daily[(pipeline_name, date)]

    def check_before_call(self, pipeline_name: str, date: str) -> None:
        spent = self.daily_spend(pipeline_name, date)
        if spent >= self.sla.max_daily_cost_usd:
            v = SLAViolation("daily", pipeline_name, spent, self.sla.max_daily_cost_usd,
                             f"{pipeline_name} paused: ${spent:.2f} >= ${self.sla.max_daily_cost_usd}/day")
            self.violations.append(v)
            raise CostBudgetExceeded(v.message)

    def observe(self, event: CostEvent) -> List[SLAViolation]:
        new: List[SLAViolation] = []
        self._daily[(event.pipeline_name, event.created_date)] += event.cost_usd
        if event.record_id is not None:
            key = (event.pipeline_name, event.record_id)
            before = self._record[key]
            self._record[key] += event.cost_usd
            after = self._record[key]
            limit = self.sla.max_cost_per_record_usd
            # report once, when the record first crosses the limit (retries included)
            if before <= limit < after:
                new.append(SLAViolation("per_record", event.record_id, after, limit,
                                        f"record {event.record_id} cost ${after:.6f} > ${limit}"))
        self.violations.extend(new)
        for v in new:
            logger.warning("SLA violation: %s", v.message)
        return new
