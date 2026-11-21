"""
Human review queue + feedback loop.

Reviewers are a triage system: confirm_error / accept_as_is (false positive) /
fix_upstream. Every decision is logged on the quarantine row and feeds back:
  - frequently overridden L1 rule        -> rule probably too strict
  - frequently disagreed L2 anomaly_type -> better prompt examples
  - problematic source                   -> raise sampling rate
"""

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.config import HUMAN_REVIEW_SLA_HOURS
from src.quarantine import QuarantineStore

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}


def _in_sample(key: str, rate: float) -> bool:
    if rate >= 1.0:
        return True
    h = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / 2**64
    return h < rate


class ReviewQueue:
    """Reads pending items from the quarantine store; writes decisions back."""

    def __init__(self, store: QuarantineStore, sample_rate: float = 0.10,
                 full_review_sources: Optional[set] = None):
        """sample_rate: fraction of quarantined records reviewed (article: 10%).
        full_review_sources: sources reviewed at 100% (healthcare loop is non-negotiable).
        Layer 2 flags are always queued - they are the signal we are calibrating."""
        self.store = store
        self.sample_rate = sample_rate
        self.full_review_sources = full_review_sources or set()

    def _eligible(self, row: dict) -> bool:
        if row["flagged_by_layer"] == "L2" or row["source_system"] in self.full_review_sources:
            return True
        return _in_sample(f"{row['record_id']}|{row['check_id']}", self.sample_rate)

    def pending(self, limit: Optional[int] = None) -> List[dict]:
        rows = [r for r in self.store.all() if r["reviewer_decision"] is None and self._eligible(r)]
        # Highest severity first, then high-confidence L2, then oldest first.
        rows.sort(key=lambda r: (
            SEVERITY_ORDER.get(r["severity"], 4),
            -(r["confidence"] or 0.0),
            r["flagged_timestamp"],
        ))
        return rows[:limit] if limit else rows

    def submit_decision(self, record_id: str, check_id: str, decision: str,
                        reviewer: str, notes: str = "") -> bool:
        ok = self.store.record_decision(record_id, check_id, decision, reviewer, notes)
        if not ok:
            raise KeyError(f"No quarantine row for ({record_id}, {check_id})")
        return ok

    def backlog_breaches(self, now: Optional[datetime] = None) -> List[dict]:
        """Items pending longer than the 48h human-review SLA."""
        now = now or datetime.utcnow()
        cutoff = now - timedelta(hours=HUMAN_REVIEW_SLA_HOURS)
        return [r for r in self.pending()
                if datetime.fromisoformat(r["flagged_timestamp"]) < cutoff]

    # ---- feedback loop ------------------------------------------------

    def feedback_report(self, min_reviews: int = 5, override_threshold: float = 0.5) -> dict:
        """Aggregate decisions per check / anomaly_type and emit recommendations.

        'Override' = accept_as_is (reviewer says the flag was wrong).
        """
        by_key: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_source: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in self.store.all():
            if r["reviewer_decision"] is None:
                continue
            key = (f"L1:{r['check_name']}" if r["flagged_by_layer"] == "L1"
                   else f"L2:{r['anomaly_type']}")
            by_key[key][r["reviewer_decision"]] += 1
            by_key[key]["total"] += 1
            by_source[r["source_system"]][r["reviewer_decision"]] += 1
            by_source[r["source_system"]]["total"] += 1

        recs = []
        for key, c in sorted(by_key.items()):
            if c["total"] < min_reviews:
                continue
            override_rate = c["accept_as_is"] / c["total"]
            if override_rate >= override_threshold:
                if key.startswith("L1:"):
                    recs.append({"key": key, "action": "relax_rule",
                                 "reason": f"overridden {override_rate:.0%} of the time; rule probably too strict"})
                else:
                    recs.append({"key": key, "action": "improve_prompt_examples",
                                 "reason": f"humans disagree {override_rate:.0%} of the time; add few-shot examples"})
        for src, c in sorted(by_source.items()):
            if c["total"] >= min_reviews and c["confirm_error"] / c["total"] >= 0.5:
                recs.append({"key": src, "action": "increase_sampling_rate",
                             "reason": f"{c['confirm_error'] / c['total']:.0%} of reviewed flags are confirmed errors"})
        return {
            "decisions_by_check": {k: dict(v) for k, v in by_key.items()},
            "decisions_by_source": {k: dict(v) for k, v in by_source.items()},
            "recommendations": recs,
        }

    def l2_precision(self) -> Optional[float]:
        """Precision of Layer 2 flags from reviewer decisions
        (confirm_error + fix_upstream count as agreement with the flag)."""
        rows = [r for r in self.store.all()
                if r["flagged_by_layer"] == "L2" and r["reviewer_decision"] is not None]
        if not rows:
            return None
        agreed = sum(1 for r in rows if r["reviewer_decision"] in ("confirm_error", "fix_upstream"))
        return round(agreed / len(rows), 4)
