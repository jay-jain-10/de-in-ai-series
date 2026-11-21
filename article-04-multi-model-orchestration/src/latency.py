"""Latency measurement: p50 / p95 / p99 per orchestration pattern."""

import threading
from collections import defaultdict

# Article 4 latency profiles (ms): reference numbers, not measurements of this code.
ARTICLE_PROFILES = {
    "router": {"p50": 250, "p95": 900, "p99": 1800},
    "chain": {"p50": 800, "p95": 2500, "p99": 4200},
    "fan_out": {"p50": 1500, "p95": 2000, "p99": 3000},
    "cascade": {"p50": 220, "p95": 950, "p99": 2500},
}


def percentile(values, pct: float) -> float:
    """Linear-interpolation percentile (same as numpy's default)."""
    if not values:
        raise ValueError("no values")
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    rank = (pct / 100) * (len(data) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(data) - 1)
    return float(data[lo] + (data[hi] - data[lo]) * (rank - lo))


class LatencyRecorder:
    def __init__(self):
        self._samples = defaultdict(list)
        self._lock = threading.Lock()

    def record(self, pattern: str, latency_ms: float) -> None:
        with self._lock:
            self._samples[pattern].append(latency_ms)

    def report(self, pattern: str) -> dict:
        s = self._samples.get(pattern)
        if not s:
            return {"count": 0}
        return {
            "count": len(s),
            "p50": round(percentile(s, 50), 1),
            "p95": round(percentile(s, 95), 1),
            "p99": round(percentile(s, 99), 1),
        }

    def report_all(self) -> dict:
        return {p: self.report(p) for p in self._samples}

    def compare_to_article(self, pattern: str) -> dict:
        """Measured percentiles next to the article's profile for that pattern."""
        measured = self.report(pattern)
        ref = ARTICLE_PROFILES.get(pattern, {})
        return {k: {"measured": measured.get(k), "article": ref.get(k)} for k in ("p50", "p95", "p99")}
