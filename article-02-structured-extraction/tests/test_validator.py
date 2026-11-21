import pytest

from src.models import ClauseExtraction, ContractExtraction, ExtractedText, PageText, Route
from src.validator import (
    ExtractionValidator, expect_median_confidence, expect_page_in_range, expect_payment_format,
    expect_payment_range, expect_value_in_source, route_for_confidence)

SOURCE = "Client shall pay invoices within 30 days of receipt. Either party may terminate on 60 days notice."


def et():
    return ExtractedText(pages=[PageText(page_number=1, text=SOURCE, method="PyPDF2")],
                         extraction_method="PyPDF2", confidence_score=1.0)


def clause(value="30 days", conf=0.97, page=1, span="within 30 days of receipt"):
    return ClauseExtraction(value=value, confidence=conf, page_number=page, source_span=span)


def run(config, clauses, total=1):
    rec = ContractExtraction(contract_id="c", extraction_version=2.1)
    v = ExtractionValidator(config)
    return v.validate(rec, clauses, et(), total)


@pytest.mark.parametrize("v,ok", [("30 days", True), ("3 months", True), ("about a month", False), ("net 30", False)])
def test_payment_format(v, ok):
    assert (expect_payment_format(v) is None) == ok


@pytest.mark.parametrize("v,ok", [("15 days", True), ("180 days", True), ("14 days", False),
                                   ("500 days", False), ("6 months", True), ("7 months", False)])
def test_payment_range(v, ok):
    assert (expect_payment_range(v, 15, 180) is None) == ok


def test_hallucination_detected_when_span_absent():
    assert expect_value_in_source(clause(span="net 60 terms apply"), SOURCE) == "hallucination_suspected"
    assert expect_value_in_source(clause(), SOURCE) is None
    assert expect_value_in_source(clause(span="WITHIN  30   days of receipt"), SOURCE) is None  # whitespace/case-insensitive
    assert expect_value_in_source(clause(span=None), SOURCE) == "missing_source_span"


def test_page_range():
    assert expect_page_in_range(clause(page=9), 3) == "page_reference_out_of_range"
    assert expect_page_in_range(clause(page=None), 3) == "missing_page_reference"
    assert expect_page_in_range(clause(page=2), 3) is None


@pytest.mark.parametrize("conf,route", [(0.99, Route.PRODUCTION), (0.95, Route.PRODUCTION), (0.94, Route.PRODUCTION_FLAGGED),
                                        (0.75, Route.PRODUCTION_FLAGGED), (0.74, Route.QUARANTINE),
                                        (0.5, Route.QUARANTINE), (0.3, Route.QUARANTINE), (0.0, Route.DLQ)])
def test_trust_pyramid(config, conf, route):
    assert route_for_confidence(conf, config) == route


def test_clean_high_confidence_goes_to_production(config):
    r = run(config, {"payment_terms": clause()})
    assert r.route == Route.PRODUCTION and not r.review_required and r.flags == []


def test_medium_confidence_flagged_but_served(config):
    r = run(config, {"payment_terms": clause(conf=0.8)})
    assert r.route == Route.PRODUCTION_FLAGGED


def test_below_0_7_requires_review(config):
    r = run(config, {"payment_terms": clause(conf=0.6)})
    assert r.route == Route.QUARANTINE and r.review_required


def test_hallucination_overrides_high_confidence(config):
    r = run(config, {"payment_terms": clause(conf=0.98, span="made up text")})
    assert r.route == Route.QUARANTINE and r.review_required
    assert "payment_terms:hallucination_suspected" in r.flags


def test_out_of_range_payment_quarantined(config):
    r = run(config, {"payment_terms": clause(value="500 days", span="within 30 days of receipt")})
    assert r.route == Route.QUARANTINE and "payment_terms:payment_terms_out_of_range" in r.flags


def test_min_confidence_across_clauses_drives_route(config):
    r = run(config, {"payment_terms": clause(conf=0.99),
                     "termination_conditions": clause("60 days notice", 0.8, 1, "terminate on 60 days notice")})
    assert r.route == Route.PRODUCTION_FLAGGED


def test_nothing_extracted_is_dlq(config):
    assert run(config, {"payment_terms": ClauseExtraction(value=None, confidence=0.0)}).route == Route.DLQ


def test_unfound_clause_does_not_block_others(config):
    r = run(config, {"payment_terms": clause(), "governing_law": ClauseExtraction(value=None, confidence=0.0)})
    assert r.route == Route.PRODUCTION


def test_apply_writes_routing_onto_record(config):
    rec = ContractExtraction(contract_id="c", extraction_version=2.1)
    res = run(config, {"payment_terms": clause(conf=0.8)})
    out = ExtractionValidator.apply(rec, res)
    assert out.routing == "production_flagged" and rec.routing == "production"


def test_median_confidence_check():
    assert expect_median_confidence([0.9, 0.8, 0.95])
    assert not expect_median_confidence([0.5, 0.6, 0.9])
    assert not expect_median_confidence([])


def test_soft_flag_downgrades_production_to_flagged(config):
    """A high-confidence clause with a bad page reference needs review, so it must
    not be served unbadged as plain 'production'."""
    r = run(config, {"payment_terms": clause(conf=0.99, page=7)}, total=3)
    assert r.route == Route.PRODUCTION_FLAGGED and r.review_required
    assert r.flags == ["payment_terms:page_reference_out_of_range"]
    r = run(config, {"payment_terms": clause(conf=0.99, page=None)})
    assert r.route == Route.PRODUCTION_FLAGGED and "payment_terms:missing_page_reference" in r.flags


def test_hallucination_check_spans_page_boundary(config):
    ext = ExtractedText(pages=[PageText(page_number=1, text="pay within thirty", method="PyPDF2"),
                               PageText(page_number=2, text="(30) days of invoice", method="PyPDF2")],
                        extraction_method="PyPDF2", confidence_score=1.0)
    c = clause(span="within thirty (30) days")
    rec = ContractExtraction(contract_id="c", extraction_version=2.1)
    r = ExtractionValidator(config).validate(rec, {"payment_terms": c}, ext, 2)
    assert "payment_terms:hallucination_suspected" not in r.flags
