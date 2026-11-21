"""Layer 1: rule parsing and deterministic syntactic checks."""

import json
from datetime import date

import pytest
import yaml

from src.rule_engine import RuleConfigError, RuleEngine, load_ruleset, parse_ruleset

TODAY = date(2026, 6, 1)


def good_claim(**over):
    c = {
        "claim_id": "C1", "patient_id": "PAT-00001", "diagnosis_code": "M17.11",
        "procedure_code": "27447", "claim_amount": 1000.0, "claim_date": "2026-05-01",
        "dosage_mg": None,
    }
    c.update(over)
    return c


def engine(ruleset, patients=None):
    return RuleEngine(ruleset, reference_data={"patients.patient_id": patients or {"PAT-00001"}}, today=TODAY)


def test_rule_file_loads_and_is_versioned(ruleset):
    assert ruleset.source == "healthcare_claims"
    assert ruleset.version == "1.0.0"
    assert any(r.rule_id == "claim_amount_range" and r.check == "BETWEEN 0 AND 1000000" for r in ruleset.rules)
    assert 3 <= len(ruleset.layer_2_examples) <= 5


def test_json_rule_file_supported(tmp_path):
    data = {"source": "s", "version": "2", "layer_1_rules": [
        {"rule_id": "a", "check": "NOT NULL", "column": "x", "severity": "critical"}]}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data))
    assert load_ruleset(str(p)).rules[0].rule_id == "a"


@pytest.mark.parametrize("bad", [
    {"version": "1", "layer_1_rules": []},
    {"source": "s", "version": "1", "layer_1_rules": [{"rule_id": "a", "check": "FOO", "column": "x"}]},
    {"source": "s", "version": "1", "layer_1_rules": [{"rule_id": "a", "check": "NOT NULL", "column": "x", "severity": "meh"}]},
    {"source": "s", "version": "1", "layer_1_rules": [
        {"rule_id": "a", "check": "NOT NULL", "column": "x"}, {"rule_id": "a", "check": "UNIQUE", "column": "y"}]},
    {"source": "s", "version": "1", "layer_1_rules": [{"rule_id": "a", "check": "REGEX (", "column": "x"}]},
])
def test_invalid_rule_files_rejected(bad):
    with pytest.raises(RuleConfigError):
        parse_ruleset(bad)


def test_clean_row_passes(ruleset):
    passed, failed, v = engine(ruleset).validate([good_claim()])
    assert len(passed) == 1 and not failed and not v


@pytest.mark.parametrize("override,rule_id", [
    ({"claim_id": None}, "claim_id_not_null"),
    ({"patient_id": ""}, "patient_id_not_null"),
    ({"patient_id": "PAT-77777"}, "patient_id_exists"),
    ({"claim_amount": -1}, "claim_amount_range"),
    ({"claim_amount": 1_000_001}, "claim_amount_range"),
    ({"claim_amount": "abc"}, "claim_amount_range"),
    ({"claim_date": "2026-07-01"}, "claim_date_valid"),
    ({"claim_date": "not-a-date"}, "claim_date_valid"),
    ({"diagnosis_code": "123"}, "diagnosis_code_format"),
    ({"procedure_code": "ABCDE"}, "procedure_code_format"),
    ({"dosage_mg": -5}, "dosage_numeric_range"),
])
def test_each_rule_catches_its_violation(ruleset, override, rule_id):
    _, failed, v = engine(ruleset).validate([good_claim(**override)])
    assert len(failed) == 1
    assert rule_id in {x.rule_id for x in v}


def test_uniqueness_flags_all_duplicates(ruleset):
    rows = [good_claim(claim_id="D"), good_claim(claim_id="D"), good_claim(claim_id="E")]
    passed, failed, v = engine(ruleset).validate(rows)
    assert len(failed) == 2 and len(passed) == 1
    assert {x.rule_id for x in v} == {"claim_id_unique"}


def test_allow_null_dosage_passes(ruleset):
    _, failed, _ = engine(ruleset).validate([good_claim(dosage_mg=None)])
    assert not failed


def test_multiple_violations_reported_per_row(ruleset):
    _, _, v = engine(ruleset).validate([good_claim(claim_amount=-1, diagnosis_code="x")])
    assert {x.rule_id for x in v} == {"claim_amount_range", "diagnosis_code_format"}


def test_semantically_wrong_record_passes_layer1(ruleset):
    """The article's point: sinusitis + knee replacement is syntactically valid."""
    row = good_claim(diagnosis_code="J01.90", procedure_code="27447")
    passed, failed, _ = engine(ruleset).validate([row])
    assert passed and not failed


def test_deterministic(ruleset):
    rows = [good_claim(claim_id=f"C{i}", claim_amount=(-1 if i % 3 == 0 else 5)) for i in range(30)]
    a = engine(ruleset).validate(rows)
    b = engine(ruleset).validate(rows)
    assert [r["claim_id"] for r in a[0]] == [r["claim_id"] for r in b[0]]
    assert a[2] == b[2]


def test_missing_reference_data_fails_closed(ruleset):
    eng = RuleEngine(ruleset, reference_data={}, today=TODAY)
    _, failed, _ = eng.validate([good_claim()])
    assert failed


def test_regex_is_full_match_trailing_newline_rejected(ruleset):
    # Python's `$` matches before a trailing newline; the engine must not.
    _, failed, v = engine(ruleset).validate([good_claim(procedure_code="27447\n")])
    assert failed and "procedure_code_format" in {x.rule_id for x in v}
