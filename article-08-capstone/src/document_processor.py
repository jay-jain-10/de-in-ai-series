"""Document processor: pluggable extractors by format, normalized text, chunking.

PDF/image extraction backends (pdfplumber, Textract, Tesseract) are injected as callables
so they can be swapped (or mocked). Errors degrade gracefully: if some pages fail we keep
what was extracted and mark extraction_confidence=0.6.
"""

import email
from dataclasses import dataclass
from email import policy
from typing import Callable, Optional, Protocol

from .config import ProcessorConfig
from .contracts import (DocumentEnvelope, ProcessedDocument, TextChunk, estimate_tokens)
import base64


class ExtractionError(Exception):
    """Nothing usable could be extracted (all pages failed / unsupported format)."""


@dataclass
class RawExtraction:
    pages: list[Optional[str]]    # None = page failed
    scanned: bool = False
    format: str = "text"


class Extractor(Protocol):
    def extract(self, content: bytes) -> RawExtraction: ...


class PlainTextExtractor:
    def extract(self, content: bytes) -> RawExtraction:
        return RawExtraction(pages=[content.decode("utf-8", errors="replace")], format="text")


class EmailExtractor:
    """Custom email parser: body text + text attachments (stdlib only)."""

    def extract(self, content: bytes) -> RawExtraction:
        msg = email.message_from_bytes(content, policy=policy.default)
        parts = []
        subject = msg.get("Subject")
        if subject:
            parts.append(f"Subject: {subject}")
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            parts.append(body.get_content())
        for att in msg.iter_attachments():
            if att.get_content_type().startswith("text/"):
                parts.append(att.get_content())
        if not parts:
            raise ExtractionError("email contained no extractable text")
        return RawExtraction(pages=["\n\n".join(str(p) for p in parts)], format="email")


class PdfExtractor:
    """Native PDFs via `native` (e.g. pdfplumber); scanned PDFs fall back to `ocr` (e.g. Textract).
    Both are callables bytes -> list[Optional[str]] (one entry per page)."""

    def __init__(self, native: Callable[[bytes], list], ocr: Optional[Callable[[bytes], list]] = None,
                 min_chars_per_page: int = 20):
        self.native, self.ocr, self.min_chars = native, ocr, min_chars_per_page

    def extract(self, content: bytes) -> RawExtraction:
        pages = self.native(content)
        text_chars = sum(len(p) for p in pages if p)
        if self.ocr and pages and text_chars < self.min_chars * len(pages):
            return RawExtraction(pages=self.ocr(content), scanned=True, format="pdf")
        return RawExtraction(pages=pages, scanned=False, format="pdf")


class ImageExtractor:
    """OCR via Tesseract / Document AI (injected callable bytes -> list[Optional[str]])."""

    def __init__(self, ocr: Callable[[bytes], list]):
        self.ocr = ocr

    def extract(self, content: bytes) -> RawExtraction:
        return RawExtraction(pages=self.ocr(content), scanned=True, format="image")


def guess_language(text: str) -> str:
    """Very rough heuristic placeholder (swap for langdetect/fastText)."""
    words = set(text.lower().split())
    for lang, markers in {"es": {"el", "la", "de", "que"}, "fr": {"le", "les", "des", "est"},
                          "de": {"der", "die", "und", "ist"}}.items():
        if len(words & markers) >= 3:
            return lang
    return "en"


class DocumentProcessor:
    def __init__(self, cfg: Optional[ProcessorConfig] = None):
        self.cfg = cfg or ProcessorConfig()
        self._extractors: dict[str, Extractor] = {}
        self.register("text/", PlainTextExtractor())
        self.register("message/rfc822", EmailExtractor())

    def register(self, mime_prefix: str, extractor: Extractor) -> None:
        self._extractors[mime_prefix] = extractor

    def _find(self, mime: str) -> Extractor:
        # longest matching prefix wins
        for prefix in sorted(self._extractors, key=len, reverse=True):
            if mime.startswith(prefix):
                return self._extractors[prefix]
        raise ExtractionError(f"no extractor registered for mime type '{mime}'")

    def process(self, envelope: DocumentEnvelope) -> ProcessedDocument:
        raw = self._find(envelope.mime_type).extract(base64.b64decode(envelope.content_b64))
        failed = [i for i, p in enumerate(raw.pages) if p is None or not p.strip()]
        good = [p for p in raw.pages if p and p.strip()]
        if not good:
            raise ExtractionError("no text could be extracted from any page")
        if failed:
            confidence = self.cfg.partial_extraction_confidence      # partial success: 0.6
        else:
            confidence = 0.9 if raw.scanned else 1.0                 # OCR'd text is less reliable
        text = "\n\n".join(good)
        return self._build(envelope.document_id, text, raw.format, len(raw.pages), raw.scanned,
                           confidence, failed)

    def from_text(self, document_id: str, text: str, fmt: str = "text",
                  extraction_confidence: float = 1.0, page_count: int = 1) -> ProcessedDocument:
        """Rebuild a ProcessedDocument from stored text (used by backfill)."""
        return self._build(document_id, text, fmt, page_count, False, extraction_confidence, [])

    def _build(self, document_id, text, fmt, pages, scanned, confidence, failed) -> ProcessedDocument:
        chunks = self.chunk(document_id, text)
        return ProcessedDocument(document_id=document_id, text=text, language=guess_language(text),
                                 format=fmt, page_count=pages, was_scanned=scanned,
                                 extraction_confidence=confidence, failed_pages=failed, chunks=chunks)

    def chunk(self, document_id: str, text: str) -> list[TextChunk]:
        """Long documents are chunked with overlap. Contract: every chunk < 4000 tokens, so we
        chunk whenever the doc would not fit (effective threshold = min(5000, 4000))."""
        c = self.cfg
        threshold = min(c.chunk_threshold_tokens, c.hard_chunk_limit_tokens - 1)
        if estimate_tokens(text) <= threshold:
            return [TextChunk(document_id=document_id, chunk_index=0, total_chunks=1, text=text,
                              token_estimate=estimate_tokens(text))]
        size, overlap = c.max_chunk_tokens * 4, c.overlap_tokens * 4
        pieces, start = [], 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):   # prefer to break on whitespace within the last 10% of the window
                cut = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
                if cut > start + int(size * 0.9):
                    end = cut
            pieces.append(text[start:end])
            if end >= len(text):
                break
            start = end - overlap
        return [TextChunk(document_id=document_id, chunk_index=i, total_chunks=len(pieces), text=p,
                          token_estimate=estimate_tokens(p)) for i, p in enumerate(pieces)]
