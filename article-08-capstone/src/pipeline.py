"""Pipeline runner + platform wiring.

Flow per document (article: ingest -> process -> extract -> validate_layer1 -> classify_risk
-> validate_layer2 -> route_to_workflow). Each stage is idempotent: re-running a document
is a no-op at ingestion (content hash), a MERGE at the warehouse, and a refresh at quarantine.
An orchestrator (Dagster/Airflow) would wrap each stage as an asset; here `run()` chains them.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import PlatformConfig
from .contracts import (ExtractedRecord, IncomingDocument, QuarantineRecord, QuarantineStage,
                        ReviewDecision, ReviewDecisionType, ValidatedRecord, ValidationIssue,
                        ValidationResult, utcnow)
from .document_processor import DocumentProcessor, ExtractionError
from .gateway.client import GatewayClient, GatewayError
from .gateway.quota import AuthRegistry
from .ingestion import IngestionService
from .lineage import LineageRecorder
from .messaging import InMemoryPublisher, Publisher
from .observability import MetricsRegistry
from .quarantine import QuarantineStore
from .registry import (ROUTE_HANDLER_FACTORIES, OnboardingReport, PipelineDefinition,
                       PipelineRegistry)
from .validation import Layer1Validator, Layer2Validator
from .warehouse import InMemoryWarehouse, Warehouse
from .domains import claims, compliance  # noqa: F401  (registers domain rules + route handlers)


@dataclass
class RunResult:
    document_id: str
    outcome: str                      # loaded | quarantined | duplicate | rejected
    quarantine_id: Optional[str] = None
    stage: Optional[str] = None
    risk_class: Optional[str] = None
    layer2_sampled: bool = False
    routed: Optional[dict] = None
    reasons: list = field(default_factory=list)


class PipelineRunner:
    def __init__(self, definition: PipelineDefinition, gateway: GatewayClient, platform: "Platform"):
        self.d, self.gw, self.p = definition, gateway, platform
        self.layer2 = Layer2Validator(gateway, definition.validation.layer2.rules,
                                      definition.validation.layer2.sample_rate,
                                      definition.validation.layer2.always_sample_risks)
        self.route = ROUTE_HANDLER_FACTORIES[definition.routing.handler](platform.publisher, definition)

    def _layer1(self, min_confidence: Optional[float] = None) -> Layer1Validator:
        v = self.d.validation
        return Layer1Validator(self.d.extraction.fields, v.layer1_rules,
                               v.min_confidence if min_confidence is None else min_confidence,
                               self.p.context)

    # ---- public entry points ---------------------------------------------------------------

    def run(self, doc: IncomingDocument) -> RunResult:
        p = self.p
        ing = p.ingestion.ingest(doc)
        if ing.duplicate:
            p.metrics.inc("pipeline_documents_total", pipeline=self.d.name, outcome="duplicate")
            return RunResult(document_id=ing.document_id, outcome="duplicate")
        env = ing.envelope
        try:
            return self._run_ingested(env)
        except Exception:
            # Anything that escapes (quota 429 re-raised for retry, gateway 5xx, a bug) must leave
            # the document retryable; otherwise the dedup store reports it as a duplicate forever.
            p.ingestion.release(env.document_id)
            p.metrics.inc("pipeline_documents_total", pipeline=self.d.name, outcome="error")
            raise

    def _run_ingested(self, env) -> RunResult:
        p = self.p
        p.lineage.record(env.document_id, "ingest", source_type=env.source_type.value,
                         source_id=env.source_id, size=env.document_size)
        try:
            processed = p.processor.process(env)
        except ExtractionError as exc:
            return self._quarantine(env.document_id, QuarantineStage.EXTRACTION, [f"processing failed: {exc}"],
                                    excerpt=base64.b64decode(env.content_b64)[:500].decode("utf-8", "replace"))
        p.lineage.record(env.document_id, "process", confidence=processed.extraction_confidence,
                         format=processed.format, chunks=len(processed.chunks),
                         failed_pages=processed.failed_pages)
        p.warehouse.merge("raw_documents", {
            "document_id": env.document_id, "team": env.team, "pipeline": env.pipeline,
            "source_type": env.source_type.value, "source_id": env.source_id,
            "received_at": env.timestamp.isoformat(), "format": processed.format, "text": processed.text,
            "extraction_confidence": processed.extraction_confidence, "page_count": processed.page_count})
        return self._extract_onward(env.document_id, processed, env.timestamp)

    def reprocess(self, document_ids: list[str]) -> list[dict]:
        """Backfill: resubmit stored raw text through the gateway and MERGE the new results."""
        out = []
        for doc_id in document_ids:
            raw = self.p.warehouse.get("raw_documents", document_id=doc_id)
            if raw is None:
                out.append({"document_id": doc_id, "outcome": "missing_raw"})
                continue
            old = self.p.warehouse.get(self.d.warehouse.table, document_id=doc_id, pipeline=self.d.name)
            processed = self.p.processor.from_text(doc_id, raw["text"], raw["format"],
                                                   raw["extraction_confidence"], raw["page_count"])
            self.p.lineage.record(doc_id, "backfill_start", previous_version=old and old["processing_version"])
            res = self._extract_onward(doc_id, processed, None)
            new = self.p.warehouse.get(self.d.warehouse.table, document_id=doc_id, pipeline=self.d.name)
            old_f, new_f = (old or {}).get("fields", {}), (new or {}).get("fields", {})
            out.append({"document_id": doc_id, "outcome": res.outcome,
                        "changed_fields": sorted(k for k in set(old_f) | set(new_f) if old_f.get(k) != new_f.get(k))})
        return out

    def stale_documents(self) -> list[str]:
        cur = self.p.lineage.processing_version
        return [r["document_id"] for r in self.p.warehouse.rows(self.d.warehouse.table)
                if r["pipeline"] == self.d.name and r["processing_version"] != cur]

    # ---- stages ------------------------------------------------------------------------------

    def _extract_onward(self, doc_id: str, processed, received_at: Optional[datetime]) -> RunResult:
        p, d = self.p, self.d
        try:
            fields, confidence = self._extract(doc_id, processed)
        except GatewayError as exc:
            if exc.status == 429:
                raise   # quota: let the orchestrator retry later; do not quarantine good documents
            return self._quarantine(doc_id, QuarantineStage.EXTRACTION, [f"gateway error: {exc}"],
                                    excerpt=processed.text)
        record = ExtractedRecord(document_id=doc_id, pipeline=d.name, team=d.team,
                                 schema_version=d.schema_version, fields=fields,
                                 extraction_confidence=confidence)

        # Layer 1 (sync)
        l1, typed = self._layer1().validate(record)
        record.fields = typed
        self._persist_validation(l1)
        p.lineage.record(doc_id, "validate_layer1", passed=l1.passed, issues=len(l1.issues))
        if not l1.passed:
            return self._quarantine(doc_id, QuarantineStage.LAYER1, [i.message for i in l1.issues],
                                    record=record, excerpt=processed.text)

        # classify risk
        risk = self.gw.classify(processed.chunks[0].text, d.risk.categories, task_type=d.risk.task_type)
        risk_class, risk_conf = risk.result["category"], risk.result["confidence"]
        self._lineage_call(doc_id, "classify_risk", risk, confidence=risk_conf, risk_class=risk_class)
        if risk.degraded:
            # The degraded default is categories[0] (e.g. "low"), which would also skip the
            # always-sample-high-risk rule. Never load a claim on a made-up risk class.
            return self._quarantine(doc_id, QuarantineStage.LAYER2,
                                    ["risk classification unavailable (gateway degraded)"],
                                    record=record, excerpt=processed.text)
        fraud = None
        if d.scoring:
            sc = self.gw.score(typed, d.scoring.logic, d.scoring.complexity, task_type=d.scoring.task_type)
            if sc.degraded:   # default 0.5 is not a score; record "unscored" rather than fake data
                self._lineage_call(doc_id, "score_fraud", sc, confidence=None, score=None)
            else:
                fraud = sc.result["score"]
                self._lineage_call(doc_id, "score_fraud", sc, confidence=None, score=fraud)
                p.warehouse.merge("fraud_scores", {"document_id": doc_id, "score": fraud,
                                                   "rationale": sc.result["rationale"], "model": sc.model})

        # Layer 2 (sampled, semantic)
        sampled = self.layer2.should_sample(doc_id, risk_class)
        if sampled:
            l2, resp = self.layer2.validate(record, typed, task_type=f"validate_{d.domain}_semantic")
            self._lineage_call(doc_id, "validate_layer2", resp, confidence=l2.confidence, passed=l2.passed)
            if resp.degraded:
                l2.passed = False
                l2.issues.append(ValidationIssue(layer=2, rule="semantic",
                                                 message="semantic validation unavailable (gateway degraded)"))
            self._persist_validation(l2)
            if not l2.passed:
                return self._quarantine(doc_id, QuarantineStage.LAYER2, [i.message for i in l2.issues],
                                        record=record, risk_class=risk_class, excerpt=processed.text)
            status = "passed"
        else:
            self._persist_validation(_not_sampled(doc_id))
            status = "not_sampled"

        validated = ValidatedRecord(record=record, risk_class=risk_class, risk_confidence=risk_conf,
                                    fraud_score=fraud, layer1_passed=True, layer2_status=status,
                                    processing_version=p.lineage.processing_version)
        return self._load_and_route(validated, received_at, sampled)

    def _extract(self, doc_id: str, processed) -> tuple[dict, float]:
        merged: dict = {}
        confs = []
        degraded = False
        for chunk in processed.chunks:
            r = self.gw.extract(chunk.text, self.d.extraction_schema, task_type=self.d.extraction.task_type)
            self._lineage_call(doc_id, "extract", r, confidence=r.result["confidence"],
                               chunk=f"{chunk.chunk_index + 1}/{chunk.total_chunks}")
            degraded |= r.degraded
            confs.append(r.result["confidence"])
            for k, v in r.result["fields"].items():
                if isinstance(v, list):
                    merged[k] = list(dict.fromkeys((merged.get(k) or []) + v))
                elif merged.get(k) in (None, "") and v not in (None, ""):
                    merged[k] = v
                else:
                    merged.setdefault(k, v)
        llm_conf = 0.0 if degraded else sum(confs) / len(confs)
        return merged, round(min(processed.extraction_confidence, llm_conf), 4)

    def _lineage_call(self, doc_id, step, resp, confidence=None, **details):
        self.p.lineage.record(doc_id, step, model_used=resp.model,
                              token_count=resp.input_tokens + resp.output_tokens, cost_usd=resp.cost_usd,
                              confidence=confidence, cached=resp.cached, degraded=resp.degraded,
                              prompt_version=resp.prompt_version, **details)

    def _persist_validation(self, r) -> None:
        self.p.warehouse.merge("validation_results", {
            "document_id": r.document_id, "layer": r.layer, "passed": r.passed,
            "issues": [i.model_dump() for i in r.issues], "sampled": r.sampled})

    def _load_and_route(self, v: ValidatedRecord, received_at: Optional[datetime], sampled: bool) -> RunResult:
        p, doc_id = self.p, v.record.document_id
        p.warehouse.merge(self.d.warehouse.table, {
            "document_id": doc_id, "pipeline": self.d.name, "team": self.d.team,
            "schema_version": v.record.schema_version, "fields": v.record.fields,
            "extraction_confidence": v.record.extraction_confidence, "risk_class": v.risk_class,
            "layer2_status": v.layer2_status, "processing_version": v.processing_version,
            "loaded_at": utcnow().isoformat()})
        p.lineage.record(doc_id, "load_warehouse", table=self.d.warehouse.table)
        routed = self.route(v)
        p.lineage.record(doc_id, "route", **routed)
        self._persist_lineage(doc_id)
        p.metrics.inc("pipeline_documents_total", pipeline=self.d.name, outcome="loaded")
        if received_at is not None:
            ra = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
            p.metrics.observe("pipeline_ingest_to_warehouse_seconds", (utcnow() - ra).total_seconds(),
                              pipeline=self.d.name)
        p.registry.record_first_output(self.d.name)
        return RunResult(document_id=doc_id, outcome="loaded", risk_class=v.risk_class,
                         layer2_sampled=sampled, routed=routed)

    def _quarantine(self, doc_id: str, stage: QuarantineStage, reasons: list[str],
                    record: Optional[ExtractedRecord] = None, risk_class: Optional[str] = None,
                    excerpt: str = "") -> RunResult:
        p = self.p
        rec = p.quarantine.add(document_id=doc_id, pipeline=self.d.name, team=self.d.team, stage=stage,
                               reasons=reasons, extracted=record.fields if record else None,
                               extraction_confidence=record.extraction_confidence if record else None,
                               risk_class=risk_class, document_excerpt=excerpt)
        p.lineage.record(doc_id, "quarantine", stage=stage.value, reasons=reasons,
                         quarantine_id=rec.quarantine_id)
        self._persist_lineage(doc_id)
        p.metrics.inc("pipeline_documents_total", pipeline=self.d.name, outcome="quarantined")
        p.metrics.inc("pipeline_quarantine_total", pipeline=self.d.name, stage=stage.value)
        return RunResult(document_id=doc_id, outcome="quarantined", quarantine_id=rec.quarantine_id,
                         stage=stage.value, risk_class=risk_class, reasons=reasons)

    def _persist_lineage(self, doc_id: str) -> None:
        for s in self.p.lineage.trace(doc_id):
            self.p.warehouse.merge("lineage", {**s.model_dump(mode="json")})

    # ---- human review release ------------------------------------------------------------------

    def release(self, rec: QuarantineRecord, decision: ReviewDecision) -> Optional[RunResult]:
        """Called when a reviewer confirms/corrects a quarantined record: re-validate (L1 structure only,
        confidence floor waived because a human vouched) and load."""
        p = self.p
        base = dict(rec.extracted or {})
        if decision.decision == ReviewDecisionType.CORRECT:
            base.update(decision.corrected_fields or {})
        if not base:
            p.metrics.inc("pipeline_release_skipped_total", pipeline=self.d.name)
            return None
        record = ExtractedRecord(document_id=rec.document_id, pipeline=self.d.name, team=self.d.team,
                                 schema_version=self.d.schema_version, fields=base, extraction_confidence=1.0)
        l1, typed = self._layer1(min_confidence=0.0).validate(record)
        p.lineage.record(rec.document_id, "human_review", reviewer=decision.reviewer,
                         decision=decision.decision.value, l1_passed=l1.passed)
        if not l1.passed:
            return self._quarantine(rec.document_id, QuarantineStage.LAYER1,
                                    ["human-reviewed record still fails layer 1: "
                                     + "; ".join(i.message for i in l1.issues)], record=record)
        record.fields = typed
        risk_class, risk_conf = rec.risk_class, 1.0
        if risk_class is None:
            raw = p.warehouse.get("raw_documents", document_id=rec.document_id)
            text = raw["text"][:12000] if raw else rec.document_excerpt
            risk = self.gw.classify(text, self.d.risk.categories, task_type=self.d.risk.task_type)
            risk_class, risk_conf = risk.result["category"], risk.result["confidence"]
            self._lineage_call(rec.document_id, "classify_risk", risk, confidence=risk_conf)
            if risk.degraded:
                return self._quarantine(rec.document_id, QuarantineStage.LAYER2,
                                        ["human-reviewed record could not be risk-classified "
                                         "(gateway degraded)"], record=record)
        v = ValidatedRecord(record=record, risk_class=risk_class, risk_confidence=risk_conf,
                            layer1_passed=True, layer2_status="human_reviewed",
                            processing_version=p.lineage.processing_version)
        return self._load_and_route(v, None, False)


def _not_sampled(doc_id: str):
    return ValidationResult(document_id=doc_id, layer=2, passed=True, sampled=False)


class Platform:
    """Bundle of shared platform services (what the platform team owns)."""

    def __init__(self, config: PlatformConfig, auth: AuthRegistry, publisher: Optional[Publisher] = None,
                 warehouse: Optional[Warehouse] = None, metrics: Optional[MetricsRegistry] = None,
                 context: Optional[dict] = None, quarantine: Optional[QuarantineStore] = None):
        self.config = config
        self.publisher = publisher or InMemoryPublisher()
        self.warehouse = warehouse or InMemoryWarehouse()
        self.metrics = metrics or MetricsRegistry()
        self.ingestion = IngestionService(self.publisher, config.kafka.incoming_topic, metrics=self.metrics)
        self.processor = DocumentProcessor(config.processor)
        self.quarantine = quarantine or QuarantineStore(config.validation.accuracy_alert_threshold)
        self.lineage = LineageRecorder(config.processing_version)
        self.registry = PipelineRegistry(auth)
        self.context = context or {}
        self.runners: dict[str, PipelineRunner] = {}
        self.quarantine.register_hook(self._on_review_event)

    def onboard(self, yaml_text: str, gateway: GatewayClient) -> OnboardingReport:
        report = self.registry.register_yaml(yaml_text, client_team=gateway.team)
        if report.ok:
            self.runners[report.pipeline] = PipelineRunner(report.definition, gateway, self)
        return report

    def set_processing_version(self, version: str) -> None:
        self.config.processing_version = version
        self.lineage.processing_version = version

    def flush_cost_events(self, cost_tracker) -> int:
        """Persist gateway cost events to the cost_events table (idempotent MERGE by event_id)."""
        for e in cost_tracker.events:
            row = e.model_dump(mode="json")
            row.pop("logged_at"); row.pop("task"); row.pop("prompt_version")
            self.warehouse.merge("cost_events", row)
        return len(cost_tracker.events)

    def _on_review_event(self, event: str, rec: QuarantineRecord, decision: Optional[ReviewDecision]) -> None:
        if event == "resolved" and decision and decision.decision != ReviewDecisionType.UPSTREAM:
            runner = self.runners.get(rec.pipeline)
            if runner:
                runner.release(rec, decision)
