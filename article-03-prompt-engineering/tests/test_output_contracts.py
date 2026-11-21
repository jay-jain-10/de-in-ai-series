import pytest
from src.output_contracts import (
    ContractViolation, SentimentOutput, VendorExtraction, find_ungrounded, parse_output,
)


def test_valid_sentiment():
    o = parse_output("SentimentOutput", '{"sentiment": "frustrated", "confidence": 0.9}')
    assert o.sentiment == "frustrated" and o.subsentiment is None


def test_optional_subsentiment_backwards_compatible():
    o = parse_output("SentimentOutput", '{"sentiment": "negative", "confidence": 0.7, "subsentiment": "angry"}')
    assert o.subsentiment == "angry"


def test_renamed_field_is_caught_not_nulled():
    # Article: Claude returns sentiment_score instead of sentiment
    with pytest.raises(ContractViolation):
        parse_output("SentimentOutput", '{"sentiment_score": "positive", "confidence": 0.9}')


def test_vendor_name_drift_is_caught():
    # Article: vendor_name / name / vendor_company rotation
    for key in ("name", "vendor_company"):
        raw = '{"%s": "Acme", "terms_days": 30, "currency": "USD", "confidence": 0.9}' % key
        with pytest.raises(ContractViolation):
            parse_output("VendorExtraction", raw)


def test_list_became_string_style_type_errors():
    with pytest.raises(ContractViolation):
        parse_output("VendorExtraction", '{"vendor_name": "A", "terms_days": "thirty", "currency": "USD", "confidence": 1}')


@pytest.mark.parametrize("raw", [
    '{"sentiment": "happy", "confidence": 0.9}',
    '{"sentiment": "positive", "confidence": 1.5}',
    '{"sentiment": "positive"}',
    "not json",
    "[1, 2]",
])
def test_rejects_bad_payloads(raw):
    with pytest.raises(ContractViolation):
        parse_output("SentimentOutput", raw)


def test_markdown_fence_is_stripped():
    raw = '```json\n{"sentiment": "neutral", "confidence": 0.6}\n```'
    assert parse_output("SentimentOutput", raw).sentiment == "neutral"


def test_unknown_contract():
    with pytest.raises(ContractViolation):
        parse_output("Nope", "{}")


def test_find_ungrounded():
    o = VendorExtraction(vendor_name="Acme Corp", terms_days=30, currency="USD", confidence=0.9)
    assert find_ungrounded(o, "Invoice from  acme   corp. Net 30", ["vendor_name"]) == []
    assert find_ungrounded(o, "Invoice from Globex", ["vendor_name"]) == ["vendor_name"]
