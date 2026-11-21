"""Critical path: parse -> Redis context -> rule score. Target <50ms p99."""

import json
import time

from pydantic import ValidationError

from src.models import Event, RuleScore
from src.rules import score_event


class CriticalPath:
    def __init__(self, feature_store, metrics, sla_tracker=None, perf_clock=time.perf_counter):
        self.store = feature_store
        self.metrics = metrics
        self.sla = sla_tracker
        self.perf_clock = perf_clock

    def parse(self, raw: str) -> Event:
        return Event(**json.loads(raw))

    def process(self, raw: str):
        """Returns (Event, RuleScore). Raises ValueError/ValidationError on bad input."""
        start = self.perf_clock()
        event = self.parse(raw)
        ctx, source = self.store.get_context(event.user_id)
        result = score_event(event, ctx)
        result.context_source = source
        result.latency_ms = (self.perf_clock() - start) * 1000.0
        # Hot-state update happens after the score is computed, off the score's latency.
        self.store.record_event(event, ctx)
        self.metrics.observe("critical_path_ms", result.latency_ms)
        if source != "redis":
            self.metrics.incr("context_fallbacks")
        if self.sla:
            self.sla.record_critical(result.latency_ms)
        return event, result
