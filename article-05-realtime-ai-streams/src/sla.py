"""Tracking for the article's three SLAs.

1. Critical path: <50ms p99
2. Enrichment:    <5s for 95% of events
3. Alert:         <10s from event to PagerDuty notification
"""

from src.config import SLAConfig
from src.metrics import percentile


class SLATracker:
    def __init__(self, config: SLAConfig = None):
        self.cfg = config or SLAConfig()
        self.critical_ms = []
        self.enrichment_s = []
        self.alert_s = []

    def record_critical(self, ms: float):
        self.critical_ms.append(ms)

    def record_enrichment(self, seconds: float):
        self.enrichment_s.append(seconds)

    def record_alert(self, seconds: float):
        self.alert_s.append(seconds)

    def report(self) -> dict:
        p99 = percentile(self.critical_ms, 99)
        within = sum(1 for s in self.enrichment_s if s < self.cfg.enrichment_seconds)
        frac = within / len(self.enrichment_s) if self.enrichment_s else 1.0
        alert_max = max(self.alert_s) if self.alert_s else 0.0
        return {
            "critical_path": {
                "p99_ms": p99,
                "target_ms": self.cfg.critical_p99_ms,
                "ok": p99 < self.cfg.critical_p99_ms,
                "count": len(self.critical_ms),
            },
            "enrichment": {
                "fraction_within": frac,
                "target_fraction": self.cfg.enrichment_fraction,
                "target_seconds": self.cfg.enrichment_seconds,
                "ok": frac >= self.cfg.enrichment_fraction,
                "count": len(self.enrichment_s),
            },
            "alert": {
                "max_seconds": alert_max,
                "target_seconds": self.cfg.alert_seconds,
                "ok": alert_max < self.cfg.alert_seconds,
                "breaches": sum(1 for s in self.alert_s if s >= self.cfg.alert_seconds),
                "count": len(self.alert_s),
            },
        }
