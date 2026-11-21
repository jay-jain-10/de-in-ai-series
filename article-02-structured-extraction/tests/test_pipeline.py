import json
from unittest.mock import MagicMock

import pytest

from src.dlq import DeadLetterQueue
from src.pipeline import ContractPipeline
from src.text_extractor import TextExtractor, TextractOCR
from tests.conftest import FakeAnthropic, clause_json, make_pdf, PAY_TEXT, TERM_TEXT, LIAB_TEXT, LAW_TEXT

SECTIONS = [
    {"title": "Payment Terms", "start_marker": "Payment. Client shall pay"},
    {"title": "Termination", "start_marker": "Termination. Either party"},
    {"title": "Liability", "start_marker": "Liability. Liability is capped"},
    {"title": "Governing Law", "start_marker": "Governing Law. This Agreement"},
]
GOOD = {
    "payment terms": clause_json("30 days", 0.97, 1, "within 30 days of receipt"),
    "termination conditions": clause_json("60 days written notice", 0.96, 2, "terminate on 60 days written notice"),
    "liability cap": clause_json("fees paid in prior 12 months", 0.95, 3, "capped at fees paid in the prior 12 months"),
    "governing law": clause_json("Delaware", 0.98, 3, "laws of Delaware"),
}


def responder_with(overrides=None):
    table = {**GOOD, **(overrides or {})}

    def respond(model, prompt):
        if "Identify the major sections" in prompt:
            return json.dumps(SECTIONS)
        for label, reply in table.items():
            if f"extracting the {label}" in prompt:
                return reply
        raise AssertionError("unexpected prompt")
    return respond


def build(config, fake_s3, responder, text_extractor=None):
    client = FakeAnthropic(responder)
    loader = MagicMock()
    dlq = DeadLetterQueue(fake_s3, config)
    te = text_extractor or TextExtractor(config)
    return ContractPipeline(client, te, dlq, loader, config), client, loader


@pytest.fixture
def pdf():
    return make_pdf([PAY_TEXT, TERM_TEXT, LIAB_TEXT + LAW_TEXT])


def test_happy_path_loads_to_production(config, fake_s3, pdf):
    p, client, loader = build(config, fake_s3, responder_with())
    res = p.process_contract("c1", pdf, "contracts/c1.pdf", contract_date="2025-01-01")
    assert res.status == "loaded" and res.route == "production"
    rec = res.record
    assert rec.payment_terms == "30 days" and rec.governing_law == "Delaware"
    assert rec.contract_date == "2025-01-01" and rec.routing == "production" and not rec.review_required
    loader.load.assert_called_once_with([rec])
    assert not fake_s3.store  # nothing quarantined
    # 1 Haiku segmentation + 4 Sonnet extractions
    assert len(client.calls) == 5 and client.calls[0]["model"] == config.anthropic.segmentation_model
    assert p.tracker.requests == 5


def test_hallucination_goes_to_quarantine_table_not_dlq(config, fake_s3, pdf):
    bad = clause_json("45 days", 0.98, 1, "net 45 days from invoice")  # span not in source
    p, _, loader = build(config, fake_s3, responder_with({"payment terms": bad}))
    res = p.process_contract("c2", pdf, "contracts/c2.pdf")
    assert res.status == "quarantined" and res.record.review_required
    assert "payment_terms:hallucination_suspected" in res.record.validation_flags
    loader.load.assert_called_once()  # loader routes it to quarantine table
    assert not fake_s3.store


def test_unparseable_claude_output_goes_to_dlq(config, fake_s3, pdf):
    p, _, loader = build(config, fake_s3, lambda m, pr: json.dumps(SECTIONS) if "Identify" in pr else "garbage")
    res = p.process_contract("c3", pdf, "contracts/c3.pdf")
    assert res.status == "dlq" and res.failure_stage == "extraction"
    err = json.loads(fake_s3.store["quarantine/c3/error.json"])
    assert err["failure_stage"] == "extraction" and err["attempt_count"] == 3
    assert err["last_attempt_model"] == config.anthropic.extraction_model
    loader.load.assert_not_called()


def test_unreadable_ocr_goes_to_dlq_with_scores(config, fake_s3):
    scanned = make_pdf([None, None])
    client = MagicMock()
    client.start_document_analysis.return_value = {"JobId": "j"}
    client.get_document_analysis.return_value = {"JobStatus": "SUCCEEDED", "Blocks": [
        {"BlockType": "LINE", "Page": 1, "Text": "smudge smudge", "Confidence": 40.0},
        {"BlockType": "LINE", "Page": 2, "Text": "smudge smudge", "Confidence": 30.0}]}
    te = TextExtractor(config, textract=TextractOCR(client, sleep=lambda s: None))
    p, claude, _ = build(config, fake_s3, responder_with(), te)
    res = p.process_contract("c4", scanned, "contracts/c4.pdf")
    assert res.status == "dlq" and res.failure_stage == "ocr"
    err = json.loads(fake_s3.store["quarantine/c4/error.json"])
    assert err["ocr_confidence_scores"] == [0.4, 0.3]
    assert claude.calls == []  # never wasted tokens on unreadable text


def test_corrupt_pdf_goes_to_dlq(config, fake_s3):
    p, _, _ = build(config, fake_s3, responder_with())
    res = p.process_contract("c5", b"nope", "contracts/c5.pdf")
    assert res.status == "dlq" and res.failure_stage == "format_detection"


def test_unexpected_error_still_dead_letters(config, fake_s3, pdf):
    te = MagicMock()
    te.extract.side_effect = ValueError("boom")
    p, _, _ = build(config, fake_s3, responder_with(), te)
    res = p.process_contract("c6", pdf, "contracts/c6.pdf")
    assert res.status == "dlq" and res.failure_stage == "text_extraction"


def test_hybrid_document_end_to_end(config, fake_s3):
    hybrid = make_pdf([PAY_TEXT, TERM_TEXT, None])
    client = MagicMock()
    client.start_document_analysis.return_value = {"JobId": "j"}
    client.get_document_analysis.return_value = {"JobStatus": "SUCCEEDED", "Blocks": [
        {"BlockType": "LINE", "Page": 3, "Text": "Liability. Liability is capped at fees paid in the prior 12 months. Governing Law. This Agreement is governed by the laws of Delaware.", "Confidence": 97.0}]}
    te = TextExtractor(config, textract=TextractOCR(client, sleep=lambda s: None))
    p, _, loader = build(config, fake_s3, responder_with(), te)
    res = p.process_contract("c7", hybrid, "contracts/c7.pdf")
    assert res.status == "loaded" and res.record.extraction_method == "Mixed(PyPDF2+Textract)"
    assert res.record.ocr_confidence == pytest.approx(0.97)


def test_batch_single_merge_and_counts(config, fake_s3, pdf):
    p, _, loader = build(config, fake_s3, responder_with())
    out = p.process_batch([
        {"contract_id": "a", "pdf_bytes": pdf, "s3_key": "contracts/a.pdf"},
        {"contract_id": "b", "pdf_bytes": b"bad", "s3_key": "contracts/b.pdf"},
    ])
    assert (out["loaded"], out["quarantined"], out["dlq"]) == (1, 0, 1)
    assert out["median_confidence_ok"] is True
    loader.load.assert_called_once()
    assert len(loader.load.call_args.args[0]) == 1
