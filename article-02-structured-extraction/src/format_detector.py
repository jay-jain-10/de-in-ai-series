"""Stage 1: Format detection.

Reads the PDF page-by-page and classifies every page as digital (text layer
present) or scanned (image only), so hybrid documents can be routed per page.
"""

import io
import logging
import re
from typing import List, Tuple

from PyPDF2 import PdfReader

from src.config import PipelineConfig
from src.models import DocumentMetadata, PageKind, PipelineFailure

logger = logging.getLogger(__name__)

_EN_STOPWORDS = {"the", "and", "of", "to", "in", "shall", "agreement", "party", "is", "for", "be"}
_NON_EN_HINTS = {
    "de": {"der", "die", "und", "das", "vertrag", "nicht"},
    "fr": {"le", "la", "les", "des", "contrat", "est"},
    "es": {"el", "los", "las", "contrato", "que", "por"},
}
_FULL_PAGE_CHARS = 3000  # a densely typed page, used to normalise density to 0-1


def detect_language(text: str) -> str:
    """Cheap stop-word heuristic. Returns an ISO-ish code or 'unknown'."""
    words = re.findall(r"[a-zA-Zäöüéèàáñ]+", text.lower())
    if len(words) < 5:
        return "unknown"
    scores = {"en": sum(w in _EN_STOPWORDS for w in words)}
    for lang, hints in _NON_EN_HINTS.items():
        scores[lang] = sum(w in hints for w in words)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def classify_pages(pdf_bytes: bytes, min_chars: int = 50) -> Tuple[List[PageKind], List[int], str]:
    """Return (kind per page, chars per page, concatenated sample text)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        n = len(reader.pages)
    except Exception as e:  # corrupt header, encrypted, not a PDF...
        raise PipelineFailure("format_detection", f"Unreadable PDF: {e}")

    kinds, chars, sample = [], [], []
    for page in reader.pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        chars.append(len(text))
        kinds.append(PageKind.DIGITAL if len(text) >= min_chars else PageKind.SCANNED)
        if text:
            sample.append(text[:500])
    if n == 0:
        raise PipelineFailure("format_detection", "PDF has zero pages")
    return kinds, chars, " ".join(sample)


def detect_format(document_id: str, pdf_bytes: bytes, config: PipelineConfig = None) -> DocumentMetadata:
    """Stage 1 entry point. Produces the metadata described in the article."""
    config = config or PipelineConfig()
    if not pdf_bytes.lstrip()[:5] == b"%PDF-":
        raise PipelineFailure("format_detection", "Missing %PDF- header")

    kinds, chars, sample = classify_pages(pdf_bytes, config.min_chars_per_digital_page)
    digital = [i + 1 for i, k in enumerate(kinds) if k == PageKind.DIGITAL]
    scanned = [i + 1 for i, k in enumerate(kinds) if k == PageKind.SCANNED]
    density = sum(min(c / _FULL_PAGE_CHARS, 1.0) for c in chars) / len(chars)

    return DocumentMetadata(
        document_id=document_id,
        total_pages=len(kinds),
        pages_digital=len(digital),
        pages_scanned=len(scanned),
        digital_page_numbers=digital,
        scanned_page_numbers=scanned,
        detected_language=detect_language(sample),
        file_size=len(pdf_bytes),
        detected_text_density=round(density, 4),
    )
