r"""Hybrid pipeline: critical path (rules) + async AI enrichment path.

Flow per event:
  Kafka -> parse -> Redis context -> rule score -> publish score -> commit
        \-> (sampled) micro-batch queue -> Claude Haiku -> idempotent upsert
              -> Redis profile update -> alert router (score > 80)
"""

import json
import logging
import time

from src.alerts import AlertRouter
from src.backpressure import BackpressureController
from src.batch_queue import MicroBatchQueue
from src.config import PipelineConfig
from src.cost_guard import CostGuard
from src.critical_path import CriticalPath
from src.enricher import AIEnricher, EnrichmentUnavailable
from src.metrics import Metrics
from src.models import EnrichmentRecord
from src.sla import SLATracker

logger = logging.getLogger(__name__)


class StreamingPipeline:
    def __init__(self, consumer, producer, feature_store, enrichment_store, enricher,
                 config: PipelineConfig = None, clock=time.time, metrics: Metrics = None,
                 sla: SLATracker = None, cost_guard: CostGuard = None,
                 perf_clock=time.perf_counter):
        self.cfg = config or PipelineConfig()
        self.clock = clock
        self.consumer = consumer
        self.producer = producer
        self.store = feature_store
        self.enrichment_store = enrichment_store
        self.enricher = enricher
        self.metrics = metrics or Metrics()
        self.sla = sla or SLATracker(self.cfg.sla)
        self.cost_guard = cost_guard or CostGuard(self.cfg.cost, self.cfg.anthropic, clock=clock)
        self.backpressure = BackpressureController(self.cfg.backpressure, self.cfg.batch, self.metrics)
        self.queue = MicroBatchQueue(self.cfg.batch.max_events, self.cfg.batch.max_wait_seconds, clock=clock)
        self.alerts = AlertRouter(producer, self.cfg.kafka.alerts_topic, self.cfg.alert_score_threshold,
                                  self.sla, clock, self.metrics)
        self.critical = CriticalPath(feature_store, self.metrics, self.sla, perf_clock)
        # Enricher shares the cost guard so every API call is metered.
        if self.enricher is not None and getattr(self.enricher, "cost_guard", None) is None:
            self.enricher.cost_guard = self.cost_guard
        self._last_event_ts = None

    # ── one poll cycle ───────────────────────────────────────────────
    def run_once(self, max_messages: int = 100) -> int:
        lag = self.consumer.lag()
        lag_seconds = max(0.0, self.clock() - self._last_event_ts) if (self._last_event_ts and lag > 0) else 0.0
        self.backpressure.update(lag, lag_seconds)
        self.queue.configure(self.backpressure.max_events, self.backpressure.max_wait)

        messages = self.consumer.poll(max_messages)
        for msg in messages:
            self._handle(msg)
        if messages:
            self.consumer.commit()  # after critical path result is published
        self._drain(force=False)
        return len(messages)

    def _handle(self, msg):
        try:
            event, result = self.critical.process(msg.value)
        except Exception as e:  # malformed events go to the DLQ, never block the stream
            self.metrics.incr("dlq")
            self.producer.produce(self.cfg.kafka.dlq_topic, msg.key, msg.value)
            logger.warning("bad event sent to DLQ: %s", e)
            return
        self._last_event_ts = event.event_ts
        self.metrics.incr("events_scored")
        self.producer.produce(self.cfg.kafka.scores_topic, event.user_id, result.model_dump_json())

        rate = self.backpressure.sample_rate
        if not self.cost_guard.should_sample(event.event_id, rate):
            self.metrics.incr("events_sampled_out")
            return
        if not self.queue.put((event, result)):
            self.metrics.incr("enrichment_shed")
        self.metrics.gauge("batch_queue_size", len(self.queue))

    # ── async enrichment path ────────────────────────────────────────
    def _drain(self, force: bool):
        while True:
            batch = self.queue.take(force=force)
            if not batch:
                break
            self._enrich(batch)
            self.metrics.gauge("batch_queue_size", len(self.queue))

    def flush(self):
        """Force out whatever is queued (shutdown / test helper)."""
        self._drain(force=True)

    def _enrich(self, batch: list):
        version = self.cfg.enrichment_version
        # Idempotency: skip events already enriched at this version (replays/retries).
        # Also de-duplicate within the batch: a redelivered event can land in the same
        # batch as its original, which would otherwise upsert twice and page twice.
        todo, seen = [], set()
        for event, result in batch:
            if event.event_id in seen or self.enrichment_store.exists(event.event_id, version):
                self.metrics.incr("enrichment_skipped_duplicate")
            else:
                seen.add(event.event_id)
                todo.append(event)
        if not todo:
            return
        items = [(e, self.store.get_context(e.user_id)[0]) for e in todo]
        try:
            enriched = self.enricher.enrich_batch(items)
        except EnrichmentUnavailable as e:
            # Rule-based score already shipped; enrichment is additive.
            self.metrics.incr("enrichment_failed_events", len(todo))
            logger.warning("enrichment skipped for %d events: %s", len(todo), e)
            return
        except Exception:
            # Any other enricher bug must not kill the consumer loop (offsets are already
            # committed, so an exception here would silently lose the rest of the queue).
            self.metrics.incr("enrichment_failed_events", len(todo))
            self.metrics.incr("enrichment_errors")
            logger.exception("unexpected enrichment error for %d events", len(todo))
            return
        stale = self.backpressure.stale
        for event in todo:
            enr = enriched.get(event.event_id)
            if enr is None:
                self.metrics.incr("enrichment_missing")
                continue
            now = self.clock()
            rec = EnrichmentRecord(
                event_id=event.event_id, enrichment_version=version, user_id=event.user_id,
                risk_score=enr.risk_score, anomaly_type=enr.anomaly_type,
                confidence=enr.confidence, reasoning=enr.reasoning,
                stale_context=stale, enriched_at=now,
            )
            self.enrichment_store.upsert(rec)
            self.store.update_risk_profile(event.user_id, {
                "risk_score": enr.risk_score, "anomaly_type": enr.anomaly_type,
                "confidence": enr.confidence, "version": version,
            })
            self.sla.record_enrichment(now - event.event_ts)
            self.metrics.incr("events_enriched")
            if stale:
                self.metrics.incr("stale_context_enrichments")
            self.alerts.route(event, rec)

    def report(self) -> dict:
        return {
            "metrics": self.metrics.snapshot(),
            "sla": self.sla.report(),
            "spend_usd": round(self.cost_guard.spend_usd, 6),
            "sample_rate": self.cost_guard.sample_rate(),
            "backpressure": self.backpressure.level,
        }
