"""Stage 5: Validation layer + confidence-based routing.

Implements the checks the article assigns to Great Expectations as
expectation-style pure functions (no GE dependency, so they run in a Lambda
and in offline tests): schema/page-reference, value range, format regex,
hallucination (span must appear in source), and a batch-level confidence
distribution check.

Failed validations never raise: they add flags and push the record toward
human review. Trust pyramid:
    >= 0.95        PRODUCTION
    0.75 - 0.95    PRODUCTION_FLAGGED  (badge: human validation recommended)
    0.5  - 0.75    QUARANTINE          (not served until reviewed)
    0 / nothing    DLQ
"""

import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.config import PipelineConfig
from src.models import ClauseExtraction, ContractExtraction, ExtractedText, Route

PAYMENT_RE = re.compile(r"^\s*(\d+)\s*(days?|months?)\s*$", re.IGNORECASE)
DAYS_PER_MONTH = 30


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


# -- expectation-style checks (each returns a flag string or None) --------------

def expect_payment_format(value: str) -> Optional[str]:
    return None if PAYMENT_RE.match(value) else "payment_terms_format_invalid"


def expect_payment_range(value: str, lo: int, hi: int) -> Optional[str]:
    m = PAYMENT_RE.match(value)
    if not m:
        return None  # format check already reports it
    n, unit = int(m.group(1)), m.group(2).lower()
    days = n * DAYS_PER_MONTH if unit.startswith("month") else n
    return None if lo <= days <= hi else "payment_terms_out_of_range"


def expect_value_in_source(clause: ClauseExtraction, source_text: str) -> Optional[str]:
    """Hallucination test: the supporting span must exist in the document."""
    if not clause.source_span:
        return "missing_source_span"
    if _norm(clause.source_span) not in _norm(source_text):
        return "hallucination_suspected"
    return None


def expect_page_in_range(clause: ClauseExtraction, total_pages: int) -> Optional[str]:
    if clause.page_number is None:
        return "missing_page_reference"
    return None if 1 <= clause.page_number <= total_pages else "page_reference_out_of_range"


def expect_median_confidence(confidences: List[float], floor: float = 0.75) -> bool:
    """Batch-level early warning for accuracy degradation."""
    return bool(confidences) and statistics.median(confidences) > floor


# -- result ---------------------------------------------------------------------

@dataclass
class ValidationResult:
    route: Route
    flags: List[str] = field(default_factory=list)
    review_required: bool = False
    clause_flags: Dict[str, List[str]] = field(default_factory=dict)


def route_for_confidence(conf: float, cfg: PipelineConfig) -> Route:
    if conf <= 0.0:
        return Route.DLQ
    if conf >= cfg.high_confidence:
        return Route.PRODUCTION
    if conf >= cfg.medium_confidence:
        return Route.PRODUCTION_FLAGGED
    return Route.QUARANTINE  # includes (0, 0.5): lowest quarantine band


class ExtractionValidator:
    def __init__(self, config: PipelineConfig = None):
        self.config = config or PipelineConfig()

    def validate(self, record: ContractExtraction, clauses: Dict[str, ClauseExtraction],
                 extracted: ExtractedText, total_pages: int) -> ValidationResult:
        cfg = self.config
        source = " ".join(p.text for p in extracted.pages)
        clause_flags: Dict[str, List[str]] = {}
        found = {k: c for k, c in clauses.items() if c.found}

        for name, c in found.items():
            flags = []
            if name == "payment_terms":
                flags.append(expect_payment_format(c.value))
                flags.append(expect_payment_range(
                    c.value, cfg.payment_terms_min_days, cfg.payment_terms_max_days))
            flags.append(expect_value_in_source(c, source))
            flags.append(expect_page_in_range(c, total_pages))
            flags = [f for f in flags if f]
            if flags:
                clause_flags[name] = flags

        all_flags = sorted({f"{k}:{f}" for k, fl in clause_flags.items() for f in fl})

        if not found:
            return ValidationResult(Route.DLQ, ["no_clauses_extracted"], True, clause_flags)

        min_conf = min(c.confidence for c in found.values())
        route = route_for_confidence(min_conf, cfg)
        hard = [f for fl in clause_flags.values() for f in fl
                if f in ("hallucination_suspected", "payment_terms_out_of_range",
                         "payment_terms_format_invalid", "missing_source_span")]
        if hard and route in (Route.PRODUCTION, Route.PRODUCTION_FLAGGED):
            route = Route.QUARANTINE  # failed validation => not served until reviewed
        elif all_flags and route == Route.PRODUCTION:
            # Soft flags (missing / out-of-range page reference) still need review;
            # serving the row unbadged as "production" would hide that from users.
            route = Route.PRODUCTION_FLAGGED

        review = min_conf < cfg.confidence_threshold or bool(all_flags) or route == Route.QUARANTINE
        return ValidationResult(route, all_flags, review, clause_flags)

    @staticmethod
    def apply(record: ContractExtraction, result: ValidationResult) -> ContractExtraction:
        return record.model_copy(update={
            "routing": result.route.value,
            "review_required": result.review_required,
            "validation_flags": result.flags,
        })
