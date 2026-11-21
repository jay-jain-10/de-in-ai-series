from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

import src.domains.claims  # noqa: F401  (registers rules)
from src.contracts import (ExtractedRecord, QuarantineStage, ReviewDecision, ReviewDecisionType,
                           ValidatedRecord)
from src.quarantine import QuarantineStore
from src.registry import PipelineDefinition
from src.validation import Layer1Validator, Layer2Validator

from .conftest import CLAIMS_YAML

DEF = PipelineDefinition(**yaml.safe_load(CLAIMS_YAML))
CTX = {"today": date(2026, 11, 21), "policy_lookup": lambda p: {"coverage_limit": 100000} if p == "POL-123456" else None}


def good_fields(**over):
    f = {"claimant_name": "Jane Doe", "policy_number": "POL-123456", "incident_date": "2026-10-01",
         "claim_amount": "4,500.00", "procedure_code": "99213", "diagnosis_code": "J20.9",
         "medical_records_referenced": [], "witnesses": []}
    f.update(over)
    return f


def l1(fields, confidence=0.95):
    rec = ExtractedRecord(document_id="d", pipeline="p", team="claims", schema_version="v1",
                          fields=fields, extraction_confidence=confidence)
    v = Layer1Validator(DEF.extraction.fields, DEF.validation.layer1_rules, 0.7, CTX)
    return v.validate(rec)


def test_layer1_passes_and_coerces_types():
    res, typed = l1(good_fields())
    assert res.passed and typed["claim_amount"] == 4500.0 and typed["incident_date"] == "2026-10-01"


@pytest.mark.parametrize("over,rule", [
    ({"claimant_name": None}, "required_fields"),
    ({"claim_amount": "lots"}, "types"),
    ({"incident_date": "yesterday"}, "types"),
    ({"claim_amount": "-5"}, "ranges"),
    ({"claim_amount": "2000000"}, "ranges"),
    ({"policy_number": "XYZ"}, "patterns"),
    ({"procedure_code": "99999"}, "claims.procedure_code_valid"),
    ({"incident_date": "2027-01-01"}, "claims.incident_not_future"),
    ({"policy_number": "POL-999999"}, "claims.policy_exists"),
    ({"claim_amount": "250000"}, "claims.amount_logic"),
    ({"claim_amount": "0"}, "claims.amount_logic"),
])
def test_layer1_failures(over, rule):
    res, _ = l1(good_fields(**over))
    assert not res.passed and rule in {i.rule for i in res.issues}
    assert all(i.layer == 1 for i in res.issues)


def test_layer1_low_confidence_fails():
    res, _ = l1(good_fields(), confidence=0.6)
    assert not res.passed and res.issues[0].rule == "min_confidence"


def test_layer1_unknown_rule_rejected_at_construction():
    with pytest.raises(ValueError):
        Layer1Validator(DEF.extraction.fields, ["nope.rule"])


def test_layer1_is_deterministic():
    a, _ = l1(good_fields(claim_amount="-1"))
    b, _ = l1(good_fields(claim_amount="-1"))
    assert a.model_dump() == b.model_dump()


def test_layer2_sampling_is_deterministic_and_near_configured_rate():
    v = Layer2Validator(None, ["r"], sample_rate=0.1)
    ids = [f"doc-{i}" for i in range(5000)]
    picks = [v.should_sample(i) for i in ids]
    assert picks == [v.should_sample(i) for i in ids]
    assert 0.07 < sum(picks) / len(ids) < 0.13


def test_layer2_always_samples_configured_risk_classes():
    v = Layer2Validator(None, ["r"], sample_rate=0.0, always_sample_risks=["high"])
    assert v.should_sample("x", "high") and not v.should_sample("x", "low")


def test_layer2_uses_gateway_validate_and_maps_issues():
    gw = SimpleNamespace(validate=lambda rec, rules, task_type: SimpleNamespace(
        result={"valid": False, "issues": ["procedure/diagnosis mismatch"], "confidence": 0.8}, degraded=False))
    rec = ExtractedRecord(document_id="d", pipeline="p", team="t", schema_version="v1", fields={},
                          extraction_confidence=1)
    res, _ = Layer2Validator(gw, ["r"]).validate(rec, {})
    assert not res.passed and res.layer == 2 and res.issues[0].message.startswith("procedure")


def test_validated_record_contract_blocks_layer1_failures():
    rec = ExtractedRecord(document_id="d", pipeline="p", team="t", schema_version="v1", fields={},
                          extraction_confidence=1)
    with pytest.raises(ValidationError):
        ValidatedRecord(record=rec, risk_class="low", risk_confidence=0.9, layer1_passed=False,
                        layer2_status="passed", processing_version="v")
    with pytest.raises(ValidationError):
        ValidatedRecord(record=rec, risk_class="low", risk_confidence=0.9, layer1_passed=True,
                        layer2_status="failed", processing_version="v")


# ---- quarantine / human review --------------------------------------------------------------

def add(store, doc="d1", **kw):
    args = dict(document_id=doc, pipeline="claims-processing", team="claims", stage=QuarantineStage.LAYER1,
                reasons=["bad amount"], extracted={"claim_amount": 5.0}, extraction_confidence=0.8,
                document_excerpt="Claimant: Jane")
    args.update(kw)
    return store.add(**args)


def decide(store, rec, kind, reviewer="r1", **kw):
    return store.resolve(ReviewDecision(quarantine_id=rec.quarantine_id, reviewer=reviewer, decision=kind, **kw))


def test_quarantine_add_is_idempotent_per_pending_document():
    s = QuarantineStore()
    a, b = add(s), add(s, reasons=["other reason"])
    assert a.quarantine_id == b.quarantine_id and len(s.pending()) == 1 and b.reasons == ["other reason"]


def test_review_view_has_what_the_ui_needs():
    s = QuarantineStore()
    rec = add(s)
    v = s.review_view(rec.quarantine_id)
    assert v["document"] == "Claimant: Jane" and v["extracted"] == {"claim_amount": 5.0}
    assert v["extraction_confidence"] == 0.8 and v["failure_reasons"] == ["bad amount"]
    assert v["actions"] == ["confirm", "correct", "upstream"]


def test_queue_is_oldest_first_and_claims_are_exclusive():
    t = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    s = QuarantineStore(clock=lambda: t["now"])
    first = add(s, "a")
    t["now"] += timedelta(minutes=5)
    add(s, "b")
    assert s.next_for_review("r1").quarantine_id == first.quarantine_id
    assert s.next_for_review("r2").document_id == "b"
    assert s.next_for_review("r3") is None


def test_hooks_fire_on_quarantine_and_resolve():
    events = []
    s = QuarantineStore()
    s.register_hook(lambda e, rec, d: events.append((e, rec.document_id, d and d.decision)))
    rec = add(s)
    decide(s, rec, ReviewDecisionType.CONFIRM)
    assert events == [("quarantined", "d1", None), ("resolved", "d1", ReviewDecisionType.CONFIRM)]


def test_cannot_resolve_twice_and_correct_requires_fields():
    s = QuarantineStore()
    rec = add(s)
    decide(s, rec, ReviewDecisionType.CONFIRM)
    with pytest.raises(ValueError):
        decide(s, rec, ReviewDecisionType.CONFIRM)
    with pytest.raises(ValidationError):
        ReviewDecision(quarantine_id="x", reviewer="r", decision=ReviewDecisionType.CORRECT)


def test_accuracy_ignores_upstream_and_alerts_below_95_percent():
    s = QuarantineStore(accuracy_alert_threshold=0.95)
    assert s.extraction_accuracy() is None and not s.needs_prompt_review()
    for i in range(18):
        decide(s, add(s, f"c{i}"), ReviewDecisionType.CONFIRM)
    decide(s, add(s, "u"), ReviewDecisionType.UPSTREAM)
    assert s.extraction_accuracy() == 1.0 and not s.needs_prompt_review()
    decide(s, add(s, "x1"), ReviewDecisionType.CORRECT, corrected_fields={"claim_amount": 7.0})
    decide(s, add(s, "x2"), ReviewDecisionType.CORRECT, corrected_fields={"claim_amount": 8.0})
    assert s.extraction_accuracy() == 0.9 and s.needs_prompt_review()     # 18/20
    assert s.corrected_field_counts()["claim_amount"] == 2
    assert s.reviewer_stats()["r1"]["total"] == 21


def test_overdue_detection_for_48h_sla():
    t = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    s = QuarantineStore(clock=lambda: t["now"])
    add(s)
    t["now"] += timedelta(hours=47)
    assert s.overdue(48 * 3600) == []
    t["now"] += timedelta(hours=2)
    assert len(s.overdue(48 * 3600)) == 1
