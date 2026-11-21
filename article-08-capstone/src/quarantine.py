"""Quarantine + human review workflow.

Quarantined records (failed L1/L2, low confidence, failed extraction) wait in a review queue.
A reviewer sees: original document excerpt, extracted data, confidence, failure reasons, and
decides confirm / correct / upstream. Decisions are logged as feedback and drive accuracy
metrics (monthly accuracy < 95% => revisit prompts).
"""

import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Callable, Optional

from .contracts import (QuarantineRecord, QuarantineStage, ReviewDecision, ReviewDecisionType,
                        utcnow)

Hook = Callable[[str, QuarantineRecord, Optional[ReviewDecision]], None]


class QuarantineStore:
    def __init__(self, accuracy_alert_threshold: float = 0.95, clock: Callable[[], datetime] = utcnow):
        self._records: dict[str, QuarantineRecord] = {}
        self._decisions: list[tuple[QuarantineRecord, ReviewDecision]] = []
        self._hooks: list[Hook] = []
        self._claimed: dict[str, str] = {}
        self.threshold, self._clock = accuracy_alert_threshold, clock

    # ---- hooks (notify UI / Slack / ticketing; the runner subscribes to release records) ----
    def register_hook(self, hook: Hook) -> None:
        self._hooks.append(hook)

    def _fire(self, event: str, rec: QuarantineRecord, decision: Optional[ReviewDecision] = None) -> None:
        for h in self._hooks:
            h(event, rec, decision)

    # ---- intake ------------------------------------------------------------------------------
    def add(self, *, document_id: str, pipeline: str, team: str, stage: QuarantineStage,
            reasons: list[str], extracted: Optional[dict] = None,
            extraction_confidence: Optional[float] = None, risk_class: Optional[str] = None,
            document_excerpt: str = "") -> QuarantineRecord:
        # idempotent: re-running a failing document refreshes the same quarantine entry
        for rec in self._records.values():
            if rec.document_id == document_id and rec.pipeline == pipeline and rec.status == "pending":
                rec.reasons, rec.stage, rec.extracted = reasons, stage, extracted
                return rec
        rec = QuarantineRecord(
            quarantine_id=uuid.uuid4().hex[:12], document_id=document_id, pipeline=pipeline, team=team,
            stage=stage, reasons=reasons, extracted=extracted, extraction_confidence=extraction_confidence,
            risk_class=risk_class, document_excerpt=document_excerpt[:2000], created_at=self._clock())
        self._records[rec.quarantine_id] = rec
        self._fire("quarantined", rec)
        return rec

    # ---- review queue ------------------------------------------------------------------------
    def get(self, quarantine_id: str) -> QuarantineRecord:
        return self._records[quarantine_id]

    def pending(self, team: Optional[str] = None) -> list[QuarantineRecord]:
        return sorted((r for r in self._records.values()
                       if r.status == "pending" and team in (None, r.team)), key=lambda r: r.created_at)

    def next_for_review(self, reviewer: str) -> Optional[QuarantineRecord]:
        """Oldest unclaimed pending record (oldest-first protects the 48h SLA)."""
        for r in self.pending():
            if r.quarantine_id not in self._claimed:
                self._claimed[r.quarantine_id] = reviewer
                return r
        return None

    def review_view(self, quarantine_id: str) -> dict:
        """Exactly what the review UI renders."""
        r = self.get(quarantine_id)
        return {"quarantine_id": r.quarantine_id, "document": r.document_excerpt, "extracted": r.extracted,
                "extraction_confidence": r.extraction_confidence, "failure_reasons": r.reasons,
                "stage": r.stage.value, "risk_class": r.risk_class,
                "actions": [d.value for d in ReviewDecisionType]}

    def resolve(self, decision: ReviewDecision) -> QuarantineRecord:
        rec = self.get(decision.quarantine_id)
        if rec.status != "pending":
            raise ValueError(f"quarantine record {rec.quarantine_id} already resolved")
        rec.status = "resolved"
        self._claimed.pop(rec.quarantine_id, None)
        self._decisions.append((rec, decision))
        self._fire("resolved", rec, decision)
        return rec

    # ---- metrics / learning loop -------------------------------------------------------------
    def reviewed(self) -> list[tuple[QuarantineRecord, ReviewDecision]]:
        return list(self._decisions)

    def extraction_accuracy(self) -> Optional[float]:
        """confirm / (confirm + correct). 'upstream' decisions say nothing about extraction quality."""
        judged = [d for _, d in self._decisions if d.decision != ReviewDecisionType.UPSTREAM]
        if not judged:
            return None
        return round(sum(d.decision == ReviewDecisionType.CONFIRM for d in judged) / len(judged), 4)

    def needs_prompt_review(self) -> bool:
        acc = self.extraction_accuracy()
        return acc is not None and acc < self.threshold

    def corrected_field_counts(self) -> Counter:
        """Which fields do humans correct most? -> where to improve prompts / few-shot examples."""
        c: Counter = Counter()
        for rec, d in self._decisions:
            if d.decision == ReviewDecisionType.CORRECT and rec.extracted:
                for k, v in (d.corrected_fields or {}).items():
                    if rec.extracted.get(k) != v:
                        c[k] += 1
        return c

    def reviewer_stats(self) -> dict:
        stats: dict[str, Counter] = defaultdict(Counter)
        for _, d in self._decisions:
            stats[d.reviewer][d.decision.value] += 1
        return {r: {**c, "total": sum(c.values())} for r, c in stats.items()}

    def reason_breakdown(self) -> Counter:
        return Counter(rec.stage.value for rec in self._records.values())

    def overdue(self, max_age_seconds: float) -> list[QuarantineRecord]:
        now = self._clock()
        return [r for r in self.pending() if (now - r.created_at).total_seconds() > max_age_seconds]
