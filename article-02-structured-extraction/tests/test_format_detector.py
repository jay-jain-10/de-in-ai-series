import pytest

from src.format_detector import detect_format, detect_language
from src.models import PipelineFailure
from tests.conftest import PAY_TEXT, make_pdf


def test_digital_document(digital_pdf, config):
    meta = detect_format("c1", digital_pdf, config)
    assert (meta.total_pages, meta.pages_digital, meta.pages_scanned) == (3, 3, 0)
    assert meta.doc_type == "digital" and not meta.is_hybrid
    assert meta.file_size == len(digital_pdf)
    assert meta.detected_language == "en"
    assert 0 < meta.detected_text_density <= 1


def test_scanned_document(config):
    meta = detect_format("c2", make_pdf([None, None]), config)
    assert meta.pages_scanned == 2 and meta.doc_type == "scanned"
    assert meta.detected_text_density == 0


def test_hybrid_document_segmented_per_page(config):
    meta = detect_format("c3", make_pdf([PAY_TEXT, None, PAY_TEXT, None]), config)
    assert meta.is_hybrid and meta.doc_type == "hybrid"
    assert meta.digital_page_numbers == [1, 3]
    assert meta.scanned_page_numbers == [2, 4]


def test_missing_header_raises(config):
    with pytest.raises(PipelineFailure) as e:
        detect_format("bad", b"not a pdf", config)
    assert e.value.stage == "format_detection"


def test_corrupt_pdf_raises(config):
    with pytest.raises(PipelineFailure):
        detect_format("bad", b"%PDF-1.4\ngarbage", config)


def test_language_heuristics():
    assert detect_language("the party shall pay the fees in the agreement and to the vendor") == "en"
    assert detect_language("der Vertrag und die Zahlung das ist nicht die") == "de"
    assert detect_language("hi") == "unknown"
