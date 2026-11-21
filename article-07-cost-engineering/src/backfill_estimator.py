"""Backfill cost estimator: records x avg_tokens x model price, with approval gate.

Article: "Before reprocessing 100K records with a new prompt, estimate ... Get
approval before running." Historical averages come from cost_events rows.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from src.config import CostSLAConfig
from src.pricing import compute_cost_usd


class BackfillNotApproved(RuntimeError):
    pass


@dataclass
class BackfillEstimate:
    records: int
    avg_input_tokens: float
    avg_output_tokens: float
    model: str
    batch: bool
    cache_hit_rate: float
    estimated_cost_usd: float
    cost_per_record_usd: float
    exceeds_record_sla: bool
    exceeds_daily_budget: bool

    @property
    def requires_approval(self) -> bool:
        return self.exceeds_record_sla or self.exceeds_daily_budget


def estimate_backfill(
    records: int, avg_input_tokens: float, avg_output_tokens: float, model: str,
    batch: bool = False, cache_hit_rate: float = 0.0, sla: CostSLAConfig = None,
) -> BackfillEstimate:
    sla = sla or CostSLAConfig()
    per_record = compute_cost_usd(model, avg_input_tokens, avg_output_tokens, batch)
    total = per_record * records * (1 - cache_hit_rate)
    return BackfillEstimate(
        records=records, avg_input_tokens=avg_input_tokens, avg_output_tokens=avg_output_tokens,
        model=model, batch=batch, cache_hit_rate=cache_hit_rate,
        estimated_cost_usd=round(total, 2),
        cost_per_record_usd=per_record * (1 - cache_hit_rate),
        exceeds_record_sla=per_record * (1 - cache_hit_rate) > sla.max_cost_per_record_usd,
        exceeds_daily_budget=total > sla.max_daily_cost_usd,
    )


def estimate_from_history(rows: Iterable[dict], records: int, model: str, **kw) -> BackfillEstimate:
    """Extrapolate from historical cost_events rows (non-cache-hit calls only).

    cost_events has one row per API *call*, retries included. A backfill pays
    for retries too, so averages are per *record*: paid tokens are summed and
    divided by the number of distinct record_ids (falling back to the call
    count for rows without a record_id). Averaging per call would understate
    the estimate by the retry factor.
    """
    paid = [r for r in rows if not r.get("cache_hit")]
    if not paid:
        raise ValueError("No paid historical calls to extrapolate from")
    ids = {r.get("record_id") for r in paid if r.get("record_id") is not None}
    n_records = len(ids) + sum(1 for r in paid if r.get("record_id") is None)
    avg_in = sum(r["input_tokens"] for r in paid) / n_records
    avg_out = sum(r["output_tokens"] for r in paid) / n_records
    return estimate_backfill(records, avg_in, avg_out, model, **kw)


def require_approval(est: BackfillEstimate, approved_by: Optional[str] = None) -> None:
    """Gate: raise unless approved when the estimate breaches an SLA."""
    if est.requires_approval and not approved_by:
        raise BackfillNotApproved(
            f"Backfill of {est.records:,} records estimated at ${est.estimated_cost_usd:,.2f} "
            f"exceeds cost SLAs; approval required.")
