"""Tiny in-process metrics: counters, gauges and latency percentiles."""

import math
from collections import defaultdict


def percentile(values, pct: float) -> float:
    """Nearest-rank percentile. Returns 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


class Metrics:
    def __init__(self):
        self.counters = defaultdict(int)
        self.gauges = {}
        self.samples = defaultdict(list)

    def incr(self, name: str, n: int = 1):
        self.counters[name] += n

    def gauge(self, name: str, value: float):
        self.gauges[name] = value

    def observe(self, name: str, value: float):
        self.samples[name].append(value)

    def pct(self, name: str, p: float) -> float:
        return percentile(self.samples[name], p)

    def snapshot(self) -> dict:
        out = {"counters": dict(self.counters), "gauges": dict(self.gauges)}
        for name, vals in self.samples.items():
            out[name] = {
                "p50": percentile(vals, 50),
                "p95": percentile(vals, 95),
                "p99": percentile(vals, 99),
                "count": len(vals),
            }
        return out
