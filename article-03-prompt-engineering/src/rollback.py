"""Automated rollback: breach during the 7-day window => move the pointer back.

Rollback is just a registry pointer update (YAML + S3 publish), so the next
DAG run picks up the previous version. Rows already written keep their
``prompt_version`` for lineage; reprocessing is a separate, explicit step.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from .config import GovernanceConfig
from .monitoring import Alert, DriftMetrics, evaluate_alerts
from .prompt_registry import PromptRegistry, S3RegistryStore

logger = logging.getLogger(__name__)


@dataclass
class RollbackDecision:
    prompt_name: str
    rolled_back: bool
    from_version: str
    to_version: Optional[str]
    reasons: List[str]
    elapsed_seconds: float = 0.0
    within_sla: bool = True


class AutoRollback:
    def __init__(self, registry: PromptRegistry, store: Optional[S3RegistryStore] = None,
                 local_path: Optional[str] = None, cfg: Optional[GovernanceConfig] = None, now=None):
        self.registry = registry
        self.store = store
        self.local_path = local_path
        self.cfg = cfg or GovernanceConfig()
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def in_monitoring_window(self, prompt_name: str) -> bool:
        activated = self.registry.last_activated_at(prompt_name)
        if activated is None:
            return False
        return self._now() - activated <= dt.timedelta(days=self.cfg.monitoring_window_days)

    def evaluate_and_rollback(self, metrics: DriftMetrics, alerts: Optional[List[Alert]] = None) -> RollbackDecision:
        name = metrics.prompt_name
        current = self.registry.active_version(name)
        alerts = alerts if alerts is not None else evaluate_alerts(metrics, self.cfg)
        reasons = [a.message for a in alerts if a.triggers_rollback]

        # Only roll back the version the metrics were measured on.
        if metrics.current_version != current:
            return RollbackDecision(name, False, current, None, ["metrics are for a different version than active"])
        if not reasons:
            return RollbackDecision(name, False, current, None, [])
        if not self.in_monitoring_window(name):
            return RollbackDecision(name, False, current, None,
                                    ["breach outside the monitoring window; manual review required"] + reasons)

        start = time.monotonic()
        target = self.registry.rollback(name, reason="; ".join(reasons))
        if self.local_path:
            self.registry.save(self.local_path)
        if self.store is not None:
            self.store.publish(self.registry)
        elapsed = time.monotonic() - start
        return RollbackDecision(name, True, current, target, reasons, elapsed,
                                elapsed < self.cfg.rollback_sla_seconds)
