"""Backpressure controller and lag metrics.

Article:
  - lag > 5000 events: widen batches (10 evts/2s -> 50 evts/5s)
  - lag higher still: sample only 50% of events for AI validation
  - lag > 5 seconds is a data quality signal: enrichments use stale context
"""

from src.config import BackpressureConfig, BatchConfig


class BackpressureController:
    def __init__(self, bp: BackpressureConfig = None, batch: BatchConfig = None, metrics=None):
        self.bp = bp or BackpressureConfig()
        self.batch = batch or BatchConfig()
        self.metrics = metrics
        self.max_events = self.batch.max_events
        self.max_wait = self.batch.max_wait_seconds
        self.sample_rate = 1.0
        self.stale = False
        self.level = "normal"

    def update(self, lag_events: int, lag_seconds: float = 0.0) -> str:
        """Re-evaluate from current lag. Returns level: normal|high|critical."""
        if lag_events > self.bp.lag_events_critical:
            level = "critical"
        elif lag_events > self.bp.lag_events_high or lag_seconds > self.bp.lag_seconds_high:
            level = "high"
        else:
            level = "normal"

        if level == "normal":
            self.max_events, self.max_wait = self.batch.max_events, self.batch.max_wait_seconds
            self.sample_rate = 1.0
        else:
            self.max_events = self.batch.wide_max_events
            self.max_wait = self.batch.wide_max_wait_seconds
            self.sample_rate = self.bp.critical_sample_rate if level == "critical" else 1.0

        self.stale = lag_seconds > self.bp.lag_seconds_high or level != "normal"
        self.level = level
        if self.metrics:
            self.metrics.gauge("consumer_lag_events", lag_events)
            self.metrics.gauge("consumer_lag_seconds", lag_seconds)
            self.metrics.gauge("backpressure_level", {"normal": 0, "high": 1, "critical": 2}[level])
        return level
