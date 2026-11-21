"""
Risk-based (stratified) sampling for Layer 2.

    score > 80   -> high   -> 100% of records
    score 50-80  -> medium -> 20%
    score < 50   -> low    -> 3%

Sampling is deterministic (hash of record_id + salt), so a retried Airflow task
selects exactly the same records.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from src.config import (
    HIGH_RISK_MIN_SCORE,
    MEDIUM_RISK_MIN_SCORE,
    SAMPLING_RATES,
)

logger = logging.getLogger(__name__)

# Domains that are always routed to 100% sampling regardless of score.
ALWAYS_FULL_CRITICALITY = {"safety_critical", "financial_critical"}


@dataclass
class SourceStats:
    source_system: str
    error_rate_per_1000: float          # human-confirmed errors per 1000 records
    business_criticality: int = 3       # 1 (low) .. 5 (safety critical)
    consequence_severity: int = 3       # 1 (low) .. 5 (patient harm)
    baseline_error_rate_per_1000: Optional[float] = None


def compute_source_risk_score(stats: SourceStats) -> float:
    """Score 0-100 = error rate x business criticality x consequences.

    error_rate_per_1000 is normalised so 10 errors/1000 at criticality 3 and
    consequence 3 (9) maps to ~72; each factor multiplies, capped at 100.
    The same formula is mirrored in dbt_models/staging/source_risk_score.sql.
    """
    raw = stats.error_rate_per_1000 * stats.business_criticality * stats.consequence_severity * 0.8
    return round(min(100.0, max(0.0, raw)), 2)


def tier_for_score(score: float) -> str:
    if score > HIGH_RISK_MIN_SCORE:
        return "high"
    if score >= MEDIUM_RISK_MIN_SCORE:
        return "medium"
    return "low"


def tier_for_source(stats: SourceStats, criticality: Optional[str] = None) -> str:
    """Tier with source-degradation escalation and criticality override.

    If a source's error rate spikes (>=2x its baseline), bump it up one tier.
    """
    if criticality in ALWAYS_FULL_CRITICALITY:
        return "high"
    tier = tier_for_score(compute_source_risk_score(stats))
    base = stats.baseline_error_rate_per_1000
    if base is not None and base > 0 and stats.error_rate_per_1000 >= 2 * base:
        tier = {"low": "medium", "medium": "high", "high": "high"}[tier]
        logger.warning(f"Source {stats.source_system} degraded; tier bumped to {tier}")
    return tier


def sampling_rate(tier: str) -> float:
    return SAMPLING_RATES[tier]


def _bucket(record_id: str, salt: str) -> float:
    digest = hashlib.sha256(f"{salt}:{record_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def default_must_include(record: dict) -> bool:
    """Stratification rule: ICU/emergency records are always validated."""
    return str(record.get("care_unit", "")).upper() in {"ICU", "EMERGENCY"}


class RiskBasedSampler:
    def __init__(
        self,
        salt: str = "",
        must_include: Callable[[dict], bool] = default_must_include,
        id_column: str = "claim_id",
        rates: Optional[Dict[str, float]] = None,
    ):
        self.salt = salt
        self.must_include = must_include
        self.id_column = id_column
        self.rates = rates or SAMPLING_RATES

    def sample(self, records: Iterable[dict], tier: str) -> List[dict]:
        rate = self.rates[tier]
        out = []
        for r in records:
            if rate >= 1.0 or self.must_include(r):
                out.append(r)
            elif _bucket(str(r[self.id_column]), self.salt) < rate:
                out.append(r)
        return out

    def sample_with_routing(
        self, records_by_tier: Dict[str, List[dict]]
    ) -> Dict[str, List[dict]]:
        return {t: self.sample(recs, t) for t, recs in records_by_tier.items()}


def estimate_monthly_cost(counts: Dict[str, int], cost_per_record: float = 0.003) -> dict:
    """Article math: 1M high + 4M medium + 5M low -> ~$5,850 (~$6K)."""
    unknown = set(counts) - set(SAMPLING_RATES)
    if unknown:
        # Otherwise those records inflate total_records (and full_validation_cost)
        # while contributing $0 to the sampled cost.
        raise ValueError(f"Unknown risk tier(s): {sorted(unknown)}")
    sampled = {t: counts.get(t, 0) * SAMPLING_RATES[t] for t in SAMPLING_RATES}
    cost = {t: round(n * cost_per_record, 2) for t, n in sampled.items()}
    total_records = sum(counts.values())
    total = round(sum(cost.values()), 2)
    return {
        "sampled_records": sampled,
        "cost_by_tier": cost,
        "total_cost": total,
        "cost_per_record_avg": round(total / total_records, 6) if total_records else 0.0,
        "full_validation_cost": round(total_records * cost_per_record, 2),
    }
