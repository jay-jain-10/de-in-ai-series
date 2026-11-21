import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.contracts import (DocumentEnvelope, IncomingDocument, SourceType, TextChunk, estimate_tokens)
from src.document_processor import (DocumentProcessor, ExtractionError, ImageExtractor, PdfExtractor)
from src.ingestion import IngestionService
from src.messaging import InMemoryPublisher, KafkaPublisher

from .conftest import make_doc


def envelope(doc: IncomingDocument, doc_id="d1") -> DocumentEnvelope:
    return DocumentEnvelope(document_id=doc_id, source_type=doc.source_type, source_id=doc.source_id,
                            document_size=doc.document_size, timestamp=doc.timestamp, team=doc.team,
                            pipeline=doc.pipeline, mime_type=doc.mime_type, content_b64=doc.content_b64)


# ---- ingestion -------------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["source_type", "source_id", "timestamp"])
def test_document_missing_contract_field_is_rejected(missing):
    payload = {"source_type": "email", "source_id": "s1", "timestamp": datetime.now(timezone.utc).isoformat(),
               "team": "claims", "pipeline": "p", "content_b64": base64.b64encode(b"hi").decode()}
    payload.pop(missing)
    svc = IngestionService(InMemoryPublisher())
    res = svc.ingest_raw(payload)
    assert not res.accepted and res.errors[0]["field"] == missing
    assert svc.publisher.messages == {}
    assert svc.metrics.counter("ingest_documents_total", outcome="rejected") == 1


@pytest.mark.parametrize("src", list(SourceType))
def test_all_five_sources_are_accepted(src):
    assert len(list(SourceType)) == 5
    pub = InMemoryPublisher()
    res = IngestionService(pub).ingest(make_doc(source=src))
    assert res.accepted
    msg = pub.values("documents.incoming")[0]
    assert msg["source_type"] == src.value and msg["document_size"] > 0 and "timestamp" in msg


def test_content_hash_dedup_is_idempotent():
    pub = InMemoryPublisher()
    svc = IngestionService(pub)
    a = svc.ingest(make_doc(source_id="a"))
    b = svc.ingest(make_doc(source_id="b-different-source-same-content"))
    assert a.accepted and b.duplicate and a.document_id == b.document_id
    assert len(pub.values("documents.incoming")) == 1
    assert not svc.ingest(make_doc(text="other content")).duplicate


def test_dedup_is_per_pipeline():
    svc = IngestionService(InMemoryPublisher())
    assert svc.ingest(make_doc(pipeline="p1")).accepted
    assert svc.ingest(make_doc(pipeline="p2")).accepted


def test_kafka_adapter_serializes_and_produces():
    producer = MagicMock()
    pub = KafkaPublisher(producer, flush_each=True)
    IngestionService(pub).ingest(make_doc())
    args = producer.produce.call_args
    assert args.args[0] == "documents.incoming" and isinstance(args.kwargs["value"], bytes)
    producer.flush.assert_called_once()


# ---- processor ---------------------------------------------------------------------------------

def test_plain_text_extraction():
    p = DocumentProcessor().process(envelope(make_doc(text="Hello claim")))
    assert p.text == "Hello claim" and p.extraction_confidence == 1.0 and len(p.chunks) == 1
    assert p.language == "en" and p.format == "text"


def test_email_parser_extracts_body_and_text_attachment():
    msg = EmailMessage()
    msg["Subject"] = "New claim"
    msg.set_content("Claimant: Jane Doe")
    msg.add_attachment("Attachment text", subtype="plain", filename="a.txt")
    doc = make_doc(data=msg.as_bytes(), mime="message/rfc822")
    p = DocumentProcessor().process(envelope(doc))
    assert "Subject: New claim" in p.text and "Jane Doe" in p.text and "Attachment text" in p.text
    assert p.format == "email"


def test_native_pdf_uses_native_extractor_only():
    native, ocr = MagicMock(return_value=["page one text is long enough", "page two text is long enough"]), MagicMock()
    proc = DocumentProcessor()
    proc.register("application/pdf", PdfExtractor(native, ocr))
    p = proc.process(envelope(make_doc(data=b"%PDF", mime="application/pdf")))
    assert not p.was_scanned and p.page_count == 2 and p.extraction_confidence == 1.0
    ocr.assert_not_called()


def test_scanned_pdf_falls_back_to_ocr():
    native, ocr = MagicMock(return_value=["", ""]), MagicMock(return_value=["ocr text page 1", "ocr text page 2"])
    proc = DocumentProcessor()
    proc.register("application/pdf", PdfExtractor(native, ocr))
    p = proc.process(envelope(make_doc(data=b"%PDF", mime="application/pdf")))
    assert p.was_scanned and "ocr text page 1" in p.text and p.extraction_confidence == 0.9
    ocr.assert_called_once()


def test_image_ocr_extractor():
    proc = DocumentProcessor()
    proc.register("image/", ImageExtractor(lambda b: ["text from image"]))
    p = proc.process(envelope(make_doc(data=b"\x89PNG", mime="image/png")))
    assert p.text == "text from image" and p.was_scanned


def test_partial_ocr_failure_keeps_text_and_marks_confidence_0_6():
    proc = DocumentProcessor()
    proc.register("image/", ImageExtractor(lambda b: ["page one ok", None, "page three ok"]))
    p = proc.process(envelope(make_doc(data=b"x", mime="image/tiff")))
    assert p.extraction_confidence == 0.6 and p.failed_pages == [1]
    assert "page one ok" in p.text and "page three ok" in p.text


def test_total_failure_and_unsupported_mime_raise():
    proc = DocumentProcessor()
    proc.register("image/", ImageExtractor(lambda b: [None, None]))
    with pytest.raises(ExtractionError):
        proc.process(envelope(make_doc(data=b"x", mime="image/png")))
    with pytest.raises(ExtractionError):
        proc.process(envelope(make_doc(data=b"x", mime="application/zip")))


def test_long_documents_are_chunked_with_overlap_under_4000_tokens():
    text = ("word " * 40000)               # ~50k tokens
    chunks = DocumentProcessor().chunk("d", text)
    assert len(chunks) > 1
    assert all(c.token_estimate < 4000 for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.total_chunks == len(chunks) and c.document_id == "d" for c in chunks)
    overlap = chunks[0].text[-200:]
    assert overlap in chunks[1].text       # overlap preserved
    assert sum(len(c.text) for c in chunks) > len(text)


def test_documents_between_4000_and_5000_tokens_are_still_chunked():
    """Contract (<4000 tokens/chunk) wins over the 5000-token chunking threshold."""
    text = "a" * (4500 * 4)
    assert estimate_tokens(text) == 4500
    chunks = DocumentProcessor().chunk("d", text)
    assert len(chunks) > 1 and all(c.token_estimate < 4000 for c in chunks)


def test_short_doc_single_chunk():
    assert len(DocumentProcessor().chunk("d", "short")) == 1


def test_chunk_contract_rejects_4000_tokens():
    with pytest.raises(ValidationError):
        TextChunk(document_id="d", chunk_index=0, total_chunks=1, text="x", token_estimate=4000)
    with pytest.raises(ValidationError):
        TextChunk(document_id="d", chunk_index=1, total_chunks=1, text="x", token_estimate=10)
