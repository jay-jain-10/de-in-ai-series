import pytest

from src.schemas import (
    ContractViolation, fallback_payload, validate_review_input, validate_task_output,
)


def test_valid_sentiment():
    out = validate_task_output("sentiment", {"sentiment": "positive", "confidence": 0.9, "reasoning": "x"})
    assert out["sentiment"] == "positive"


@pytest.mark.parametrize("payload,msg", [
    ({"confidence": 0.9, "reasoning": "x"}, "Missing field: sentiment"),
    ({"sentiment": "angry", "confidence": 0.9, "reasoning": "x"}, None),
    ({"sentiment": "positive", "confidence": 1.5, "reasoning": "x"}, None),
    ({"sentiment": "positive", "confidence": 0.5, "reasoning": "x" * 501}, None),
])
def test_invalid_sentiment_rejected(payload, msg):
    with pytest.raises(ContractViolation, match=msg or None):
        validate_task_output("sentiment", payload)


def test_non_object_rejected():
    with pytest.raises(ContractViolation):
        validate_task_output("sentiment", ["positive"])


def test_aspect_and_toxicity_contracts():
    validate_task_output("aspect_extraction", {"aspects": ["a"], "confidence": 0.5, "reasoning": "r"})
    validate_task_output("toxicity_detection", {"toxic": True, "confidence": 0.5, "reasoning": "r"})
    with pytest.raises(ContractViolation):
        validate_task_output("toxicity_detection", {"confidence": 0.5, "reasoning": "r"})


def test_input_contract():
    assert validate_review_input("ok") == "ok"
    assert len(validate_review_input("a" * 6000)) == 5000
    for bad in ("", "   ", None):
        with pytest.raises(ContractViolation):
            validate_review_input(bad)


def test_fallback_payloads():
    assert fallback_payload("sentiment") == {"sentiment": "unknown", "confidence": 0.0, "needs_review": True}
    assert fallback_payload("aspect_extraction")["needs_review"] is True
    assert fallback_payload("toxicity_detection")["toxic"] is None
