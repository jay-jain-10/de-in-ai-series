"""Layer 2 validator with a mocked Anthropic client."""

import json
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

import src.semantic_validator as sv
from src.semantic_validator import SemanticResult, SemanticValidator, format_examples
from tests.conftest import make_response

REC = {"claim_id": "C1", "diagnosis_code": "J01.90", "procedure_code": "27447",
       "diagnosis_description": "acute sinusitis", "procedure_description": "total knee replacement",
       "patient_ssn": "SHOULD-NOT-BE-SENT"}


def make_validator(monkeypatch, ruleset, create):
    client = MagicMock()
    client.messages.create.side_effect = create
    monkeypatch.setattr(sv.anthropic, "Anthropic", lambda api_key=None: client)
    v = SemanticValidator("test-key", ruleset.layer_2_examples)
    return v, client


def payload(**over):
    p = {"is_consistent": False, "anomaly_type": "diagnosis_procedure_mismatch",
         "confidence": 0.9, "reasoning": "x"}
    p.update(over)
    return p


def test_defaults_use_sonnet_model():
    from src.config import AnthropicConfig
    assert "sonnet" in AnthropicConfig().model


def test_prompt_contains_examples_and_only_whitelisted_fields(monkeypatch, ruleset):
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: make_response(payload()))
    prompt = v.build_prompt(REC)
    assert "diagnosis doesn't match procedure" in prompt
    assert "knee osteoarthritis" in prompt
    assert "SHOULD-NOT-BE-SENT" not in prompt
    assert "healthcare" in prompt


def test_flagged_when_inconsistent_and_confident(monkeypatch, ruleset):
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: make_response(payload(confidence=0.6)))
    out = v.validate_record(REC)
    assert out["flagged"] and out["validation_error"] is None
    assert out["result"].anomaly_type == "diagnosis_procedure_mismatch"


def test_not_flagged_below_healthcare_threshold(monkeypatch, ruleset):
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: make_response(payload(confidence=0.59)))
    assert v.confidence_threshold == 0.60
    assert not v.validate_record(REC)["flagged"]


def test_not_flagged_when_consistent(monkeypatch, ruleset):
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: make_response(
        payload(is_consistent=True, anomaly_type="none", confidence=0.99)))
    assert not v.validate_record(REC)["flagged"]


def test_domain_thresholds():
    from src.config import CONFIDENCE_THRESHOLDS
    assert CONFIDENCE_THRESHOLDS == {"healthcare": 0.60, "financial": 0.70, "marketing": 0.80}


def test_markdown_wrapped_json_handled(monkeypatch, ruleset):
    resp = make_response(payload())
    resp.content[0].text = "```json\n" + json.dumps(payload()) + "\n```"
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: resp)
    assert v.validate_record(REC)["flagged"]


def test_invalid_json_retries_then_degrades(monkeypatch, ruleset):
    bad = make_response({})
    bad.content[0].text = "not json"
    v, client = make_validator(monkeypatch, ruleset, lambda **k: bad)
    out = v.validate_record(REC, max_retries=3)
    assert client.messages.create.call_count == 3
    assert out["result"] is None and not out["flagged"] and out["validation_error"]
    assert v.get_cost_estimate()["total_failures"] == 1


def test_failure_rate_is_per_record_not_per_attempt(monkeypatch, ruleset):
    bad = make_response({}); bad.content[0].text = "not json"
    good = make_response(payload())
    seq = iter([bad, bad, bad, good])     # record 1: 3 failed attempts; record 2: ok
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: next(seq))
    v.validate_record(REC, max_retries=3)
    v.validate_record(REC, max_retries=3)
    c = v.get_cost_estimate()
    assert c["total_requests"] == 4 and c["total_records"] == 2
    assert c["failure_rate"] == 0.5          # 1 of 2 records, not 1 of 4 attempts


def test_empty_content_degrades_not_raises(monkeypatch, ruleset):
    resp = make_response(payload()); resp.content = []
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: resp)
    out = v.validate_record(REC, max_retries=2)
    assert out["validation_error"] and not out["flagged"]


def test_schema_violation_rejected(monkeypatch, ruleset):
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: make_response(payload(confidence=1.7)))
    out = v.validate_record(REC, max_retries=1)
    assert out["validation_error"] and not out["flagged"]


def test_rate_limit_backoff_then_success(monkeypatch, ruleset):
    monkeypatch.setattr(sv.time, "sleep", lambda s: None)
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    calls = {"n": 0}

    def create(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise err
        return make_response(payload())

    v, _ = make_validator(monkeypatch, ruleset, create)
    assert v.validate_record(REC)["flagged"]
    assert calls["n"] == 2


def test_api_error_degrades_not_raises(monkeypatch, ruleset):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def create(**k):
        raise anthropic.APIError("boom", request=req, body=None)

    v, _ = make_validator(monkeypatch, ruleset, create)
    out = v.validate_record(REC, max_retries=2)
    assert "API error" in out["validation_error"]


def test_cost_estimate_uses_sonnet_pricing(monkeypatch, ruleset):
    v, _ = make_validator(monkeypatch, ruleset, lambda **k: make_response(payload(), 1_000_000, 1_000_000))
    v.validate_record(REC)
    assert v.get_cost_estimate()["estimated_cost_usd"] == 18.0


def test_format_examples_roundtrip(ruleset):
    text = format_examples(ruleset.layer_2_examples)
    assert text.count("Example ") == len(ruleset.layer_2_examples)


def test_semantic_result_schema():
    with pytest.raises(ValueError):
        SemanticResult(is_consistent=True, confidence=-0.1)
