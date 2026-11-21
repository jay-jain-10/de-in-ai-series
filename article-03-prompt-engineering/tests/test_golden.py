import pytest

from src.config import GovernanceConfig
from src.golden import (
    CaseResult, GoldenCase, SuiteResult, compare, evaluate_case, load_golden, run_suite,
)
from src.prompt_runner import PromptRunner, RunResult
from src.output_contracts import SentimentOutput, VendorExtraction
from tests.conftest import FakeClient, golden_path, oracle_json, user_text


def _run(output, contract_valid=True, text="in"):
    return RunResult("p", "1", "m", text, output=output, schema_valid=contract_valid, error="" if contract_valid else "bad")


def suite(version, passes, confs=None):
    cases = []
    for i, ok in enumerate(passes):
        cases.append(CaseResult(f"c{i}", ok, [] if ok else ["x"], (confs[i] if confs else 0.9), True))
    return SuiteResult("p", version, cases)


# ── dataset loading ───────────────────────────────────────────────────
@pytest.mark.parametrize("name,minimum", [("sentiment_classifier", 10), ("escalation_classifier", 6), ("vendor_extractor", 4)])
def test_golden_files_load(name, minimum):
    assert len(load_golden(golden_path(name))) >= minimum


def test_article_examples_present(sentiment_cases):
    by = {c.input: c for c in sentiment_cases}
    assert by["Your app crashed and lost my data"].min_confidence == 0.8
    assert by["It's fine"].expected == {"sentiment": "neutral"} and by["It's fine"].min_confidence == 0.6


# ── case evaluation ───────────────────────────────────────────────────
def test_evaluate_case_pass_and_fail_reasons():
    case = GoldenCase("c", "It's fine", expected={"sentiment": "neutral"}, min_confidence=0.6)
    ok = evaluate_case(case, _run(SentimentOutput(sentiment="neutral", confidence=0.7)), "SentimentOutput")
    assert ok.passed
    wrong = evaluate_case(case, _run(SentimentOutput(sentiment="positive", confidence=0.7)), "SentimentOutput")
    assert not wrong.passed and "sentiment" in wrong.failures[0]
    low = evaluate_case(case, _run(SentimentOutput(sentiment="neutral", confidence=0.4)), "SentimentOutput")
    assert not low.passed and "min" in low.failures[0]


def test_evaluate_case_confidence_range_and_approx():
    case = GoldenCase("c", "x", expected_approx={"escalation_risk": [0.45, 0.2]}, acceptable_confidence_range=[0.3, 0.6])
    from src.output_contracts import EscalationOutput
    good = EscalationOutput(escalation_risk=0.5, urgency="low", confidence=0.5)
    assert evaluate_case(case, _run(good), "EscalationOutput").passed
    far = EscalationOutput(escalation_risk=0.9, urgency="low", confidence=0.5)
    assert not evaluate_case(case, _run(far), "EscalationOutput").passed
    sure = EscalationOutput(escalation_risk=0.5, urgency="low", confidence=0.95)
    assert not evaluate_case(case, _run(sure), "EscalationOutput").passed


def test_evaluate_case_schema_invalid_fails():
    case = GoldenCase("c", "x")
    r = evaluate_case(case, _run(None, contract_valid=False), "SentimentOutput")
    assert not r.passed and not r.schema_valid


def test_evaluate_case_detects_hallucination():
    case = GoldenCase("c", "Invoice from Globex", expected={"terms_days": 30})
    out = VendorExtraction(vendor_name="Acme", terms_days=30, currency="USD", confidence=0.9)
    r = evaluate_case(case, _run(out), "VendorExtraction")
    assert not r.passed and any("hallucination" in f for f in r.failures)


# ── gate decisions (thresholds from the article) ──────────────────────
def test_gate_passes_identical():
    d = compare(suite("1.0", [True] * 20), suite("1.1", [True] * 20))
    assert not d.blocked and not d.green_flag


def test_gate_exactly_5_percent_fail_is_allowed():
    d = compare(suite("1.0", [True] * 20), suite("1.1", [False] + [True] * 19))  # 1/20 = 5%
    assert not d.blocked


def test_gate_blocks_over_5_percent_failures():
    d = compare(suite("1.0", [True] * 10), suite("1.1", [False] + [True] * 9))  # 10%
    assert d.blocked
    assert any("golden cases fail" in r for r in d.reasons)
    assert d.regressions == ["c0"]


def test_gate_blocks_regression_even_if_baseline_also_failing():
    # candidate fail rate 4/20 = 20% -> blocked via fail rate AND regressions
    base = suite("1.0", [False, False, False, False] + [True] * 16)
    cand = suite("1.1", [True, True, True, True] + [False] * 4 + [True] * 12)
    d = compare(base, cand)
    assert d.blocked and any("regressed" in r for r in d.reasons)


def test_gate_blocks_confidence_drop_over_005():
    base = suite("1.0", [True] * 10, [0.9] * 10)
    cand = suite("1.1", [True] * 10, [0.8] * 10)
    d = compare(base, cand)
    assert d.blocked and d.confidence_drop == pytest.approx(0.1)
    assert any("confidence" in r for r in d.reasons)


def test_gate_confidence_drop_within_tolerance_ok():
    d = compare(suite("1.0", [True] * 10, [0.9] * 10), suite("1.1", [True] * 10, [0.86] * 10))
    assert not d.blocked


def test_gate_green_flag_when_improving_more_than_5_percent():
    base = suite("1.0", [False, False] + [True] * 18)   # 10% failing
    cand = suite("1.1", [True] * 20)
    d = compare(base, cand)
    assert not d.blocked and d.green_flag and d.improvement_rate == pytest.approx(0.1)


def test_gate_thresholds_come_from_config():
    cfg = GovernanceConfig(max_golden_fail_rate=0.2)
    assert not compare(suite("1", [True] * 10), suite("2", [False] + [True] * 9), cfg).blocked


# ── end-to-end with the fake model ────────────────────────────────────
def test_run_suite_perfect_model_passes_everything(registry, perfect_client, sentiment_cases):
    res = run_suite(PromptRunner(perfect_client, registry), "sentiment_classifier", "1.3", sentiment_cases)
    assert res.fail_rate == 0.0 and res.total == len(sentiment_cases)
    assert res.avg_confidence >= 0.8


def test_run_suite_with_schema_breaking_prompt_fails_all(registry, sentiment_cases):
    client = FakeClient(lambda kw: '{"label": "positive", "confidence": 0.9}')
    res = run_suite(PromptRunner(client, registry), "sentiment_classifier", "1.3", sentiment_cases)
    assert res.fail_rate == 1.0 and all(not c.schema_valid for c in res.cases)


def test_non_numeric_approx_field_fails_case_instead_of_crashing():
    from src.output_contracts import SentimentOutput
    case = GoldenCase("c", "x", expected_approx={"sentiment": [0.5, 0.1]})
    r = evaluate_case(case, _run(SentimentOutput(sentiment="neutral", confidence=0.7)), "SentimentOutput")
    assert not r.passed and "sentiment" in r.failures[0]
