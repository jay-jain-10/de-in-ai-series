"""
Two-layer validation orchestration (pure functions; Airflow wraps these).

Idempotency: re-running run_layer1 / run_layer2 for the same input never
creates duplicate quarantine rows (UNIQUE (record_id, check_id)) and Layer 2
skips records it has already flagged (no repeat API spend on retries for
flagged records; records judged consistent are re-sent on a retry).
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from src.quarantine import QuarantineRecord, QuarantineStore
from src.risk_sampler import RiskBasedSampler, SourceStats, tier_for_source
from src.rule_engine import RuleEngine, RuleSet, record_key
from src.semantic_validator import SemanticValidator

logger = logging.getLogger(__name__)

L2_CHECK_ID = "L2:semantic_consistency"


def run_layer1(
    rows: List[dict], ruleset: RuleSet, store: QuarantineStore, run_id: str,
    reference_data: Optional[Dict[str, set]] = None, id_column: str = "claim_id",
) -> dict:
    """Layer 1 on 100% of rows. Failures -> quarantine; passes returned."""
    start = time.time()
    engine = RuleEngine(ruleset, reference_data=reference_data, id_column=id_column)
    passed, failed, violations = engine.validate(rows)
    # Same key function the engine uses, so null-id rows still get their payload.
    by_id = {record_key(r, id_column): r for r in failed}
    recs = [
        QuarantineRecord(
            record_id=v.record_id,
            source_system=ruleset.source,
            flagged_by_layer="L1",
            check_id=f"L1:{v.rule_id}",
            check_name=v.rule_id,
            severity=v.severity,
            rules_version=ruleset.version,
            run_id=run_id,
            record_payload=by_id.get(v.record_id),
        )
        for v in violations
    ]
    write = store.add_many(recs)
    return {
        "total": len(rows),
        "passed": len(passed),
        "failed": len(failed),
        "violations": len(violations),
        "quarantine_inserted": write["inserted"],
        "quarantine_skipped_duplicates": write["skipped_duplicates"],
        "duration_seconds": round(time.time() - start, 3),
        "passed_records": passed,
    }


def select_layer2_sample(
    passed_rows: List[dict], ruleset: RuleSet, sampler: RiskBasedSampler,
    source_stats: SourceStats,
) -> Tuple[str, List[dict]]:
    """Pick the risk tier for the source and draw the (deterministic) sample."""
    tier = tier_for_source(source_stats, ruleset.criticality)
    return tier, sampler.sample(passed_rows, tier)


def run_layer2(
    passed_rows: List[dict], ruleset: RuleSet, validator: SemanticValidator,
    sampler: RiskBasedSampler, store: QuarantineStore, run_id: str,
    source_stats: SourceStats, id_column: str = "claim_id",
    sample: Optional[List[dict]] = None, tier: Optional[str] = None,
) -> dict:
    """Layer 2 on a risk-based sample of Layer-1 survivors.

    Pass `sample`/`tier` to reuse a pre-selected sample (Airflow task split)."""
    start = time.time()
    if sample is None:
        tier, sample = select_layer2_sample(passed_rows, ruleset, sampler, source_stats)

    skipped_already_flagged, flagged, errors, consistent = 0, 0, 0, 0
    unvalidated_ids = []
    new_recs = []
    for row in sample:
        rid = str(row[id_column])
        if store.exists(rid, L2_CHECK_ID):
            skipped_already_flagged += 1   # retry-safe: no duplicate API call
            continue
        out = validator.validate_record(row)
        if out["validation_error"]:
            errors += 1
            unvalidated_ids.append(rid)
            continue
        if out["flagged"]:
            res = out["result"]
            flagged += 1
            new_recs.append(QuarantineRecord(
                record_id=rid, source_system=ruleset.source, flagged_by_layer="L2",
                check_id=L2_CHECK_ID, check_name="semantic_consistency",
                anomaly_type=res.anomaly_type, confidence=res.confidence,
                severity="critical" if ruleset.criticality == "safety_critical" else "high",
                rules_version=ruleset.version, run_id=run_id,
                record_payload={**row, "ai_reasoning": res.reasoning},
            ))
        else:
            consistent += 1
    store.add_many(new_recs)

    all_flagged = {r["record_id"] for r in store.all()
                   if r["check_id"] == L2_CHECK_ID and r["source_system"] == ruleset.source}
    # Sampled records whose Layer 2 call failed are NOT promoted to production:
    # they were selected for semantic validation and did not get it. They are
    # returned separately so the caller can retry/hold them.
    unvalidated = set(unvalidated_ids)
    production = [r for r in passed_rows
                  if str(r[id_column]) not in all_flagged and str(r[id_column]) not in unvalidated]
    unvalidated_records = [r for r in sample if str(r[id_column]) in unvalidated]
    return {
        "tier": tier,
        "eligible": len(passed_rows),
        "sampled": len(sample),
        "validated_consistent": consistent,
        "flagged": flagged,
        "skipped_already_flagged": skipped_already_flagged,
        "validation_errors": errors,
        "unvalidated_ids": unvalidated_ids,
        "duration_seconds": round(time.time() - start, 3),
        "cost": validator.get_cost_estimate(),
        "production_records": production,
        "unvalidated_records": unvalidated_records,
    }
