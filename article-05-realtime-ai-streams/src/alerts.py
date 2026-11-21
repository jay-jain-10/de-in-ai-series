"""Alert routing: AI risk score > 80 -> PagerDuty (via alerts topic); else store only."""

import json
import time
from typing import Optional

from src.models import EnrichmentRecord, Event


class AlertRouter:
    def __init__(self, producer, topic: str, threshold: int = 80, sla_tracker=None,
                 clock=time.time, metrics=None):
        self.producer = producer
        self.topic = topic
        self.threshold = threshold
        self.sla = sla_tracker
        self.clock = clock
        self.metrics = metrics
        self.sent = []

    def route(self, event: Event, rec: EnrichmentRecord) -> Optional[dict]:
        """Returns the alert payload if one was sent, else None."""
        if rec.risk_score <= self.threshold:
            if self.metrics:
                self.metrics.incr("stored_only")
            return None
        now = self.clock()
        alert = {
            "event_id": event.event_id,
            "user_id": event.user_id,
            "risk_score": rec.risk_score,
            "anomaly_type": rec.anomaly_type,
            "confidence": rec.confidence,
            "destination": "pagerduty",
            "event_ts": event.event_ts,
            "alerted_at": now,
        }
        self.producer.produce(self.topic, event.user_id, json.dumps(alert))
        self.sent.append(alert)
        if self.sla:
            self.sla.record_alert(now - event.event_ts)
        if self.metrics:
            self.metrics.incr("alerts_sent")
        return alert
