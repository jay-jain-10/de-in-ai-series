"""Observability: lightweight metrics registry + dashboards + SLA evaluation.

In production, export these to CloudWatch / Prometheus; the registry is deliberately tiny.
"""

import math
import threading
from collections import defaultdict
from typing import Optional


def _key(name: str, labels: dict) -> tuple:
    return (name, tuple(sorted(labels.items())))


class MetricsRegistry:
    def __init__(self):
        self._counters: dict[tuple, float] = defaultdict(float)
        self._gauges: dict[tuple, float] = {}
        self._obs: dict[tuple, list[float]] = defaultdict(list)
        self._lock = threading.Lock()   # `+=` on a dict entry is not atomic across threads

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        with self._lock:
            self._counters[_key(name, labels)] += value

    def set_gauge(self, name: str, value: float, **labels) -> None:
        self._gauges[_key(name, labels)] = value

    def observe(self, name: str, value: float, **labels) -> None:
        with self._lock:
            self._obs[_key(name, labels)].append(value)

    def counter(self, name: str, **labels) -> float:
        """Sum of all counters with this name whose labels include the given ones."""
        want = set(labels.items())
        return sum(v for (n, lab), v in self._counters.items() if n == name and want <= set(lab))

    def gauge(self, name: str, **labels) -> Optional[float]:
        return self._gauges.get(_key(name, labels))

    def observations(self, name: str, **labels) -> list[float]:
        want = set(labels.items())
        out: list[float] = []
        for (n, lab), vals in self._obs.items():
            if n == name and want <= set(lab):
                out.extend(vals)
        return out

    @staticmethod
    def percentile(values: list[float], q: float) -> Optional[float]:
        if not values:
            return None
        s = sorted(values)
        return s[min(len(s) - 1, math.ceil(q * len(s)) - 1)]

    def snapshot(self) -> dict:
        fmt = lambda n, lab: n + ("{" + ",".join(f"{k}={v}" for k, v in lab) + "}" if lab else "")
        return {
            "counters": {fmt(n, lab): v for (n, lab), v in sorted(self._counters.items())},
            "gauges": {fmt(n, lab): v for (n, lab), v in sorted(self._gauges.items())},
        }


class PlatformDashboards:
    """The four dashboard families from the architecture diagram."""

    def __init__(self, metrics: MetricsRegistry, cost_tracker=None, quarantine=None):
        self.metrics, self.cost, self.quarantine = metrics, cost_tracker, quarantine

    def cost_dashboard(self, budgets: Optional[dict] = None) -> dict:
        out = {"report": self.cost.report() if self.cost else {}}
        if self.cost and budgets:
            out["budget_alerts"] = self.cost.budget_alerts(budgets)
        calls = self.metrics.counter("gateway_requests_total")
        out["model_usage"] = {m: self.metrics.counter("gateway_llm_calls_total", model=m)
                              for m in self._label_values("gateway_llm_calls_total", "model")}
        out["cache_hit_rate"] = round(self.metrics.counter("gateway_cache_hits_total") / calls, 4) if calls else 0.0
        return out

    def quality_dashboard(self) -> dict:
        done = self.metrics.counter("pipeline_documents_total")
        quarantined = self.metrics.counter("pipeline_documents_total", outcome="quarantined")
        q = self.quarantine
        return {
            "documents_processed": done,
            "quarantine_rate": round(quarantined / done, 4) if done else 0.0,
            "quarantine_backlog": len(q.pending()) if q else 0,
            "human_review_rate": round(quarantined / done, 4) if done else 0.0,
            "extraction_accuracy": q.extraction_accuracy() if q else None,
        }

    def _label_values(self, name: str, label: str) -> set:
        return {dict(lab)[label] for (n, lab) in self.metrics._counters if n == name and label in dict(lab)}


class SLAMonitor:
    """Evaluates the four published platform SLAs. A breach should page the platform team."""

    def __init__(self, cfg, metrics: MetricsRegistry, cost_tracker=None, quarantine=None, clock=None):
        self.cfg, self.metrics, self.cost, self.quarantine, self._clock = cfg, metrics, cost_tracker, quarantine, clock

    def evaluate(self) -> dict:
        results = {}
        lat = self.metrics.observations("pipeline_ingest_to_warehouse_seconds")
        p = self.metrics.percentile(lat, self.cfg.ingest_to_warehouse_percentile)
        results["ingest_to_warehouse"] = {
            "target": f"{int(self.cfg.ingest_to_warehouse_percentile * 100)}% within {self.cfg.ingest_to_warehouse_seconds}s",
            "observed_p": p, "ok": p is None or p <= self.cfg.ingest_to_warehouse_seconds}

        total = self.metrics.counter("gateway_requests_total")
        errors = self.metrics.counter("gateway_errors_total", status="5xx")
        avail = 1.0 if total == 0 else 1 - errors / total
        results["gateway_availability"] = {"target": self.cfg.gateway_availability,
                                           "observed": round(avail, 5), "ok": avail >= self.cfg.gateway_availability}

        lag = 0.0
        if self.cost and self.cost.events:
            lag = max((e.logged_at - e.called_at).total_seconds() for e in self.cost.events)
        results["cost_tracking_lag"] = {"target": self.cfg.cost_lag_seconds, "observed": lag,
                                        "ok": lag <= self.cfg.cost_lag_seconds}

        overdue = self.quarantine.overdue(self.cfg.review_wait_seconds) if self.quarantine else []
        results["human_review"] = {"target": self.cfg.review_wait_seconds,
                                   "overdue_records": len(overdue), "ok": not overdue}
        results["all_ok"] = all(v["ok"] for v in results.values())
        return results
