from unittest.mock import MagicMock

import pytest

from src.format_detector import detect_format
from src.models import PageText, PipelineFailure
from src.text_extractor import TextExtractor, TextractOCR
from tests.conftest import PAY_TEXT, make_pdf

S3 = {"bucket": "b", "key": "k.pdf"}


def textract_blocks(page_conf):
    blocks = []
    for page, conf in page_conf.items():
        blocks.append({"BlockType": "LINE", "Page": page, "Text": f"scanned text page {page} payment 30 days", "Confidence": conf})
    blocks.append({"BlockType": "TABLE", "Page": min(page_conf)})
    return blocks


def textract_client(blocks, statuses=("IN_PROGRESS", "SUCCEEDED")):
    c = MagicMock()
    c.start_document_analysis.return_value = {"JobId": "job1"}
    seq = [{"JobStatus": s} for s in statuses[:-1]]
    seq.append({"JobStatus": statuses[-1], "Blocks": blocks})
    c.get_document_analysis.side_effect = seq
    return c


def test_digital_uses_pypdf2_only(digital_pdf, config):
    meta = detect_format("c", digital_pdf, config)
    tr = MagicMock()
    out = TextExtractor(config, textract=tr).extract(digital_pdf, meta, S3)
    assert out.extraction_method == "PyPDF2" and out.confidence_score == 1.0
    assert "30 days" in out.full_text and "[[PAGE 1]]" in out.full_text
    tr.ocr.assert_not_called()


def test_scanned_uses_textract_with_tables(config):
    pdf = make_pdf([None, None])
    meta = detect_format("c", pdf, config)
    client = textract_client(textract_blocks({1: 96.0, 2: 90.0}))
    tx = TextractOCR(client, sleep=lambda s: None)
    out = TextExtractor(config, textract=tx).extract(pdf, meta, S3)
    assert out.extraction_method == "Textract"
    assert out.table_count == 1
    assert out.confidence_score == pytest.approx(0.93)
    assert client.get_document_analysis.call_count == 2


def test_hybrid_mixed_methods(config):
    pdf = make_pdf([PAY_TEXT, None])
    meta = detect_format("c", pdf, config)
    tx = TextractOCR(textract_client(textract_blocks({2: 95.0})), sleep=lambda s: None)
    out = TextExtractor(config, textract=tx).extract(pdf, meta, S3)
    assert out.extraction_method == "Mixed(PyPDF2+Textract)"
    assert [p.page_number for p in out.pages] == [1, 2]


def test_textract_failure_falls_back_to_tesseract(config):
    pdf = make_pdf([None])
    meta = detect_format("c", pdf, config)
    bad = MagicMock()
    bad.start_document_analysis.side_effect = RuntimeError("throttled")
    tess = MagicMock(return_value=[PageText(page_number=1, text="tesseract text here", method="Tesseract", confidence=0.8)])
    out = TextExtractor(config, textract=TextractOCR(bad), tesseract_fn=tess).extract(pdf, meta, S3)
    assert out.extraction_method == "Tesseract"
    tess.assert_called_once()


def test_no_textract_configured_uses_tesseract(config):
    pdf = make_pdf([None])
    meta = detect_format("c", pdf, config)
    tess = MagicMock(return_value=[PageText(page_number=1, text="tesseract text here", method="Tesseract", confidence=0.7)])
    out = TextExtractor(config, tesseract_fn=tess).extract(pdf, meta, S3)
    assert out.extraction_method == "Tesseract"


def test_low_confidence_all_pages_is_ocr_failure(config):
    pdf = make_pdf([None, None])
    meta = detect_format("c", pdf, config)
    tx = TextractOCR(textract_client(textract_blocks({1: 42.0, 2: 38.0})), sleep=lambda s: None)
    with pytest.raises(PipelineFailure) as e:
        TextExtractor(config, textract=tx).extract(pdf, meta, S3)
    assert e.value.stage == "ocr"
    assert e.value.details["ocr_confidence_scores"] == [0.42, 0.38]


def test_both_ocr_engines_fail(config):
    pdf = make_pdf([None])
    meta = detect_format("c", pdf, config)
    with pytest.raises(PipelineFailure) as e:
        TextExtractor(config, tesseract_fn=MagicMock(side_effect=RuntimeError("no binary"))).extract(pdf, meta, S3)
    assert e.value.stage == "ocr"


def test_blank_pages_filtered(config):
    pdf = make_pdf([PAY_TEXT, None])
    meta = detect_format("c", pdf, config)
    tess = MagicMock(return_value=[PageText(page_number=2, text="  ", method="Tesseract", confidence=0.9)])
    out = TextExtractor(config, tesseract_fn=tess).extract(pdf, meta, S3)
    assert [p.page_number for p in out.pages] == [1]


def test_textract_timeout(config):
    client = MagicMock()
    client.start_document_analysis.return_value = {"JobId": "j"}
    client.get_document_analysis.return_value = {"JobStatus": "IN_PROGRESS"}
    with pytest.raises(TimeoutError):
        TextractOCR(client, max_polls=2, sleep=lambda s: None).ocr("b", "k", [1])
