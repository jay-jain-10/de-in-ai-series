"""Shared fixtures and a fake Claude client. Nothing here touches the network."""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data_generator import make_claims, make_patients  # noqa: E402
from src.quarantine import QuarantineStore  # noqa: E402
from src.rule_engine import load_ruleset  # noqa: E402

RULES_PATH = os.path.join(ROOT, "rules", "healthcare_claims_v1.yaml")


@pytest.fixture
def ruleset():
    return load_ruleset(RULES_PATH)


@pytest.fixture
def patients():
    return make_patients(50)


@pytest.fixture
def store():
    s = QuarantineStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def claims_and_truth(patients):
    return make_claims(n=300, seed=7, patients=patients)


def make_response(payload: dict, in_tok=300, out_tok=60):
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(payload))]
    resp.usage.input_tokens = in_tok
    resp.usage.output_tokens = out_tok
    return resp


def semantic_oracle(record: dict) -> dict:
    """Deterministic stand-in for Claude: encodes the same domain rules the
    few-shot examples teach, so end-to-end tests are meaningful offline."""
    if record.get("diagnosis_code") == "J01.90" and record.get("procedure_code") == "27447":
        return {"is_consistent": False, "anomaly_type": "diagnosis_procedure_mismatch",
                "confidence": 0.95, "reasoning": "sinusitis vs knee replacement"}
    if record.get("dosage_mg") and record["dosage_mg"] > 1000:
        return {"is_consistent": False, "anomaly_type": "impossible_dosage",
                "confidence": 0.9, "reasoning": "dosage impossible"}
    if record.get("discharge_date") and record["discharge_date"] < record["admit_date"]:
        return {"is_consistent": False, "anomaly_type": "temporal_inconsistency",
                "confidence": 0.85, "reasoning": "discharge before admit"}
    return {"is_consistent": True, "anomaly_type": "none", "confidence": 0.9, "reasoning": "ok"}


def oracle_side_effect(**kwargs):
    prompt = kwargs["messages"][0]["content"]
    rec_json = prompt.split("Record to evaluate:\n", 1)[1].split("\n\nReturn ONLY", 1)[0]
    return make_response(semantic_oracle(json.loads(rec_json)))


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Patch anthropic.Anthropic inside the validator; client uses the oracle."""
    import src.semantic_validator as sv
    client = MagicMock()
    client.messages.create.side_effect = oracle_side_effect
    monkeypatch.setattr(sv.anthropic, "Anthropic", lambda api_key=None: client)
    return client
