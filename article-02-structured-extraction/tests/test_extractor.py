import json

import pytest

from src.extractor import ClauseExtractor, select_chunks
from src.models import Chunk, ClauseExtraction, ExtractedText, PageText, PipelineFailure
from tests.conftest import FakeAnthropic, clause_json


def chunks():
    return [
        Chunk(chunk_id=0, title="Payment Terms", text="Client shall pay invoices within 30 days.", start_page=1, end_page=1),
        Chunk(chunk_id=1, title="Termination", text="Either party may terminate on 60 days notice.", start_page=2, end_page=2),
        Chunk(chunk_id=2, title="Liability", text="Liability cap equals fees paid.", start_page=3, end_page=3),
        Chunk(chunk_id=3, title="Governing Law", text="Governed by the laws of Delaware.", start_page=3, end_page=3),
    ]


def test_select_chunks_prefers_relevant_titles():
    assert select_chunks(chunks(), "payment_terms")[0].title == "Payment Terms"
    assert select_chunks(chunks(), "governing_law")[0].title == "Governing Law"


def test_clause_schema_enforces_confidence_range():
    with pytest.raises(Exception):
        ClauseExtraction(value="30 days", confidence=1.5)
    with pytest.raises(Exception):
        ClauseExtraction(value="30 days")  # confidence missing


def test_valid_reply_parsed_with_sonnet(config):
    client = FakeAnthropic(lambda m, p: clause_json("30 days", 0.93, 1, "within 30 days"))
    ex = ClauseExtractor(client, config).extract_clause("payment_terms", chunks())
    assert ex.value == "30 days" and ex.confidence == 0.93 and ex.page_number == 1
    assert client.calls[0]["model"] == config.anthropic.extraction_model


def test_markdown_wrapped_json_ok(config):
    client = FakeAnthropic(lambda m, p: "```json\n" + clause_json("30 days", 0.9, 1, "x") + "\n```")
    assert ClauseExtractor(client, config).extract_clause("payment_terms", chunks()).value == "30 days"


def test_missing_confidence_reprompts_with_clearer_note(config):
    replies = iter([json.dumps({"value": "30 days", "page_number": 1, "source_span": "x"}),
                    clause_json("30 days", 0.9, 1, "within 30 days")])
    client = FakeAnthropic(lambda m, p: next(replies))
    ext = ClauseExtractor(client, config)
    out = ext.extract_clause("payment_terms", chunks())
    assert out.confidence == 0.9
    assert len(client.calls) == 2
    assert "previous reply was rejected" not in client.calls[0]["prompt"]
    assert "previous reply was rejected" in client.calls[1]["prompt"]
    assert ext.attempt_counts["payment_terms"] == 2


def test_exhausted_attempts_raise_extraction_failure(config):
    client = FakeAnthropic(lambda m, p: "not json at all")
    with pytest.raises(PipelineFailure) as e:
        ClauseExtractor(client, config).extract_clause("payment_terms", chunks())
    assert e.value.stage == "extraction"
    assert e.value.details["attempt_count"] == 3
    assert len(client.calls) == 3


def test_no_relevant_chunk_returns_not_found_without_api_call(config):
    client = FakeAnthropic(lambda m, p: "unused")
    only = [Chunk(chunk_id=0, title="Misc", text="nothing here", start_page=1, end_page=1)]
    out = ClauseExtractor(client, config).extract_clause("payment_terms", only)
    assert not out.found and out.confidence == 0.0 and client.calls == []


def test_null_value_not_found(config):
    client = FakeAnthropic(lambda m, p: json.dumps({"value": None, "confidence": 0.2, "page_number": None, "source_span": None}))
    assert not ClauseExtractor(client, config).extract_clause("payment_terms", chunks()).found


def test_extract_contract_builds_lineage_record(config):
    def responder(model, prompt):
        if "payment terms" in prompt:
            return clause_json("30 days", 0.96, 1, "within 30 days")
        if "termination" in prompt:
            return clause_json("60 days notice", 0.9, 2, "60 days notice")
        if "liability" in prompt:
            return clause_json("fees paid", 0.8, 3, "equals fees paid")
        return clause_json("Delaware", 0.97, 3, "laws of Delaware")
    ext = ClauseExtractor(FakeAnthropic(responder), config)
    et = ExtractedText(pages=[PageText(page_number=1, text="t", method="Textract", confidence=0.95)],
                       extraction_method="Textract", confidence_score=0.95)
    rec, clauses = ext.extract_contract("c1", chunks(), et)
    assert set(clauses) == {"payment_terms", "termination_conditions", "liability_cap", "governing_law"}
    assert rec.governing_law == "Delaware" and rec.extraction_version == 2.1
    assert rec.extraction_method == "Textract" and rec.ocr_confidence == 0.95
    assert rec.prompt_version == config.prompt_version and rec.model_name == config.anthropic.extraction_model
    assert rec.confidence_scores["liability_cap"] == 0.8 and rec.processing_timestamp.endswith("Z")


def test_v1_config_skips_governing_law(config):
    config.extraction_version = 1.5
    assert "governing_law" not in config.clause_types
