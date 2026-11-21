"""End-to-end two-layer flow (mocked Claude), quarantine and idempotency."""

import pytest

from src.pipeline import L2_CHECK_ID, run_layer1, run_layer2, select_layer2_sample
from src.quarantine import QuarantineRecord, make_quarantine_id
from src.risk_sampler import RiskBasedSampler, SourceStats
from src.semantic_validator import SemanticValidator
from tests.conftest import make_response

STATS = SourceStats("healthcare_claims", 30, 5, 5)


def l1(claims, ruleset, store, patients, run_id="r1"):
    return run_layer1(claims, ruleset, store, run_id, reference_data={"patients.patient_id": patients})


def test_layer1_quarantines_syntactic_failures_only(claims_and_truth, ruleset, store, patients):
    claims, truth = claims_and_truth
    s = l1(claims, ruleset, store, patients)
    syntactic = {k for k, v in truth.items() if v == "syntactic_error"}
    quarantined = {r["record_id"] for r in store.all()}
    assert quarantined == syntactic
    assert s["total"] == len(claims) == s["passed"] + s["failed"]
    assert all(r["flagged_by_layer"] == "L1" and r["rules_version"] == "1.0.0" for r in store.all())


def test_layer1_idempotent_rerun(claims_and_truth, ruleset, store, patients):
    claims, _ = claims_and_truth
    first = l1(claims, ruleset, store, patients)
    n = store.count()
    second = l1(claims, ruleset, store, patients, run_id="retry")
    assert store.count() == n
    assert first["quarantine_inserted"] == n
    assert second["quarantine_inserted"] == 0
    assert second["quarantine_skipped_duplicates"] == first["violations"]


def test_layer2_catches_semantic_errors_and_routes_them(claims_and_truth, ruleset, store, patients, fake_anthropic):
    claims, truth = claims_and_truth
    passed = l1(claims, ruleset, store, patients)["passed_records"]
    v = SemanticValidator("k", ruleset.layer_2_examples)
    out = run_layer2(passed, ruleset, v, RiskBasedSampler(salt="d"), store, "r1", STATS)
    semantic = {k for k, lab in truth.items() if lab == "semantic_error"}
    flagged = {r["record_id"] for r in store.all() if r["flagged_by_layer"] == "L2"}
    assert out["tier"] == "high"            # safety critical -> 100%
    assert out["sampled"] == len(passed)
    assert flagged == semantic and semantic
    prod_ids = {r["claim_id"] for r in out["production_records"]}
    assert not prod_ids & flagged
    assert len(prod_ids) == len(passed) - len(flagged)
    row = store.get(next(iter(flagged)), L2_CHECK_ID)
    assert row["anomaly_type"] and row["confidence"] >= 0.6


def test_layer2_idempotent_no_duplicate_rows_or_api_calls(claims_and_truth, ruleset, store, patients, fake_anthropic):
    claims, _ = claims_and_truth
    passed = l1(claims, ruleset, store, patients)["passed_records"]
    v = SemanticValidator("k", ruleset.layer_2_examples)
    sampler = RiskBasedSampler(salt="d")
    run_layer2(passed, ruleset, v, sampler, store, "r1", STATS)
    n, calls = store.count(), fake_anthropic.messages.create.call_count
    out = run_layer2(passed, ruleset, v, sampler, store, "r2", STATS)
    assert store.count() == n
    assert out["flagged"] == 0 and out["skipped_already_flagged"] > 0
    # Only previously-consistent records are re-sent; flagged ones are not paid for twice.
    assert fake_anthropic.messages.create.call_count - calls == len(passed) - out["skipped_already_flagged"]


def test_layer2_failures_not_silently_passed_as_validated(ruleset, store, monkeypatch):
    import src.semantic_validator as sv
    from unittest.mock import MagicMock
    client = MagicMock()
    bad = make_response({}); bad.content[0].text = "garbage"
    client.messages.create.return_value = bad
    monkeypatch.setattr(sv.anthropic, "Anthropic", lambda api_key=None: client)
    v = SemanticValidator("k", ruleset.layer_2_examples)
    rows = [{"claim_id": "C1", "care_unit": "ICU", "diagnosis_code": "I10"}]
    out = run_layer2(rows, ruleset, v, RiskBasedSampler(), store, "r", STATS)
    assert out["validation_errors"] == 1 and out["unvalidated_ids"] == ["C1"]
    assert out["validated_consistent"] == 0 and store.count() == 0
    # Sampled-but-unvalidated records must not be promoted to production.
    assert out["production_records"] == []
    assert [r["claim_id"] for r in out["unvalidated_records"]] == ["C1"]


def test_layer1_null_id_row_keeps_payload(ruleset, store, patients):
    row = {"claim_id": None, "patient_id": "PAT-00001", "diagnosis_code": "I10",
           "procedure_code": "99213", "claim_amount": 10.0, "claim_date": "2026-05-01"}
    run_layer1([row], ruleset, store, "r", reference_data={"patients.patient_id": patients})
    q = store.get("<null>", "L1:claim_id_not_null")
    assert q is not None and q["record_payload"] is not None
    assert '"patient_id": "PAT-00001"' in q["record_payload"]


def test_low_tier_samples_subset(ruleset):
    rows = [{"claim_id": f"C{i}", "care_unit": "OUTPATIENT"} for i in range(5000)]
    import copy
    rs = copy.deepcopy(ruleset); rs.criticality = "medium"
    tier, sample = select_layer2_sample(rows, rs, RiskBasedSampler(salt="z"), SourceStats("s", 1.0, 2, 2))
    assert tier == "low" and 0 < len(sample) < 400


def test_quarantine_id_deterministic_and_unique_key(store):
    assert make_quarantine_id("A", "L1:x") == make_quarantine_id("A", "L1:x")
    assert make_quarantine_id("A", "L1:x") != make_quarantine_id("A", "L1:y")
    rec = QuarantineRecord("A", "s", "L1", "L1:x", "x")
    assert store.add(rec) is True
    assert store.add(rec) is False
    assert store.count() == 1
    # same record, different check -> separate audit rows
    assert store.add(QuarantineRecord("A", "s", "L1", "L1:y", "y"))
    assert store.count() == 2


def test_quarantine_rejects_bad_layer_and_decision(store):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.add(QuarantineRecord("A", "s", "L3", "c", "c"))
    store.add(QuarantineRecord("A", "s", "L1", "c", "c"))
    with pytest.raises(ValueError):
        store.record_decision("A", "c", "maybe", "bob")
