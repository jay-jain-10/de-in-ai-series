from src.data_generator import make_claims, make_patients
from src.rule_engine import RuleEngine
from datetime import date


def test_generator_is_deterministic_and_synthetic():
    a, _ = make_claims(50, seed=1)
    b, _ = make_claims(50, seed=1)
    assert a == b
    assert all(c["patient_id"] is None or c["patient_id"].startswith("PAT-") for c in a)


def test_semantic_errors_pass_layer1_by_construction(ruleset):
    pats = make_patients()
    claims, truth = make_claims(2000, seed=11, patients=pats)
    eng = RuleEngine(ruleset, {"patients.patient_id": pats}, today=date(2026, 6, 1))
    passed, failed, _ = eng.validate(claims)
    pids = {c["claim_id"] for c in passed}
    assert all(cid in pids for cid, l in truth.items() if l == "semantic_error")
    assert all(cid not in pids for cid, l in truth.items() if l == "syntactic_error")
    assert any(l == "semantic_error" for l in truth.values())
