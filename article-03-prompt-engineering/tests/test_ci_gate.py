import json

from src.ci_gate import main, run_gate
from src.golden import load_golden
from tests.conftest import FakeClient, ROOT, golden_path, oracle_json, user_text
import os

GOLDEN_DIR = os.path.join(ROOT, "golden")
GOOD_PROMPT = "Candidate prompt GOOD"
BAD_PROMPT = "Candidate prompt BAD"


def _candidate(registry, text, version="1.4"):
    head = registry.copy()
    schema = registry.get("sentiment_classifier").validation_schema
    head.add_version("sentiment_classifier", {
        "version": version, "model": "claude-haiku-4-5-20251001", "contract": "SentimentOutput",
        "system_prompt": text, "validation_schema": schema}, activate=True)
    return head


def _client(sentiment_cases, bad_system):
    by_input = {c.input: c for c in sentiment_cases}

    def responder(kw):
        case = by_input[user_text(kw)]
        if kw["system"] == bad_system:
            return json.dumps({"sentiment_label": "positive", "confidence": 0.9})  # field rename
        return oracle_json("sentiment_classifier", case)
    return FakeClient(responder)


def test_unchanged_registry_passes_without_model_calls(registry):
    client = FakeClient(lambda kw: 1 / 0)
    report = run_gate(registry, registry, GOLDEN_DIR, client)
    assert not report["blocked"] and report["decisions"] == [] and client.calls == []


def test_good_candidate_passes(registry, sentiment_cases):
    head = _candidate(registry, GOOD_PROMPT)
    report = run_gate(head, registry, GOLDEN_DIR, _client(sentiment_cases, BAD_PROMPT))
    assert not report["blocked"]
    d = report["decisions"][0]
    assert (d["baseline_version"], d["candidate_version"]) == ("1.3", "1.4")


def test_schema_breaking_candidate_blocks_pr(registry, sentiment_cases):
    head = _candidate(registry, BAD_PROMPT)
    report = run_gate(head, registry, GOLDEN_DIR, _client(sentiment_cases, BAD_PROMPT))
    assert report["blocked"]
    assert report["decisions"][0]["blocked"] and report["decisions"][0]["fail_rate"] == 1.0


def test_in_place_edit_blocks_statically(registry):
    head = registry.copy()
    head._data["sentiment_classifier"]["versions"]["1.3"]["system_prompt"] = "Extract vendor info. Return JSON."
    report = run_gate(head, registry, GOLDEN_DIR, client=None)
    assert report["blocked"] and any("immutable" in p for p in report["static_problems"])


def test_cli_static_only_exit_codes(tmp_path, registry):
    base = tmp_path / "base.yaml"
    base.write_text(registry.to_yaml())
    head = tmp_path / "head.yaml"
    head.write_text(registry.to_yaml())
    rep = tmp_path / "r.json"
    args = ["--registry", str(head), "--base-registry", str(base), "--golden-dir", GOLDEN_DIR,
            "--static-only", "--report", str(rep)]
    assert main(args) == 0
    assert json.loads(rep.read_text())["blocked"] is False

    bad = registry.copy()
    bad._data["vendor_extractor"]["versions"]["1.0"]["system_prompt"] = "Extract vendor info and terms. Return JSON."
    head.write_text(bad.to_yaml())
    assert main(args) == 1


def test_new_prompt_without_base_is_gated_on_absolute_failures(registry):
    base = registry.copy()
    del base._data["vendor_extractor"]
    cases = {c.input: c for c in load_golden(golden_path("vendor_extractor"))}
    client = FakeClient(lambda kw: oracle_json("vendor_extractor", cases[user_text(kw)]))
    report = run_gate(registry, base, GOLDEN_DIR, client)
    assert not report["blocked"] and report["decisions"][0]["baseline_version"] == "1.0"


def test_cli_missing_base_registry_fails_closed(tmp_path, registry):
    head = tmp_path / "head.yaml"
    head.write_text(registry.to_yaml())
    args = ["--registry", str(head), "--base-registry", str(tmp_path / "missing.yaml"),
            "--golden-dir", GOLDEN_DIR, "--static-only", "--report", str(tmp_path / "r.json")]
    assert main(args) == 2


def test_gate_runs_both_versions_and_blocks_on_regression(registry, sentiment_cases):
    """Candidate breaks only 'its_fine' (1/10 = 10% > 5%): baseline must actually be run."""
    by_input = {c.input: c for c in sentiment_cases}

    def responder(kw):
        case = by_input[user_text(kw)]
        if kw["system"] == GOOD_PROMPT and case.name == "its_fine":
            return json.dumps({"sentiment": "positive", "confidence": 0.9})
        return oracle_json("sentiment_classifier", case)
    client = FakeClient(responder)
    report = run_gate(_candidate(registry, GOOD_PROMPT), registry, GOLDEN_DIR, client)
    d = report["decisions"][0]
    assert report["blocked"] and d["regressions"] == ["its_fine"]
    systems = {c["system"] for c in client.calls}
    assert systems == {GOOD_PROMPT, registry.get("sentiment_classifier", "1.3").system_prompt}
