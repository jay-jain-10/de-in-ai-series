"""Stage 2: Text extraction with fallback.

Digital pages  -> PyPDF2 (fast, stays in Lambda).
Scanned pages  -> AWS Textract (async, S3-based, returns tables + confidence).
Textract error -> Tesseract fallback (free, slower, less accurate).
All pages OCR'd below ocr_min_confidence -> PipelineFailure('ocr') -> DLQ.
"""

import io
import logging
import time
from typing import Callable, Dict, List, Optional

from PyPDF2 import PdfReader

from src.config import PipelineConfig
from src.format_detector import detect_language
from src.models import DocumentMetadata, ExtractedText, PageText, PipelineFailure

logger = logging.getLogger(__name__)

# (pdf_bytes, page_numbers) -> list[PageText]; injectable for tests.
OcrFn = Callable[[bytes, List[int]], List[PageText]]


class TextractOCR:
    """Thin wrapper over boto3 Textract async document analysis (S3 source)."""

    def __init__(self, client, poll_seconds: float = 2.0, max_polls: int = 120, sleep=time.sleep):
        self.client = client
        self.poll_seconds = poll_seconds
        self.max_polls = max_polls
        self._sleep = sleep

    def ocr(self, bucket: str, key: str, page_numbers: List[int]) -> Dict:
        job = self.client.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES"],
        )
        job_id = job["JobId"]
        blocks: List[dict] = []
        for _ in range(self.max_polls):
            resp = self.client.get_document_analysis(JobId=job_id)
            status = resp.get("JobStatus")
            if status == "SUCCEEDED":
                blocks.extend(resp.get("Blocks", []))
                token = resp.get("NextToken")
                while token:
                    resp = self.client.get_document_analysis(JobId=job_id, NextToken=token)
                    blocks.extend(resp.get("Blocks", []))
                    token = resp.get("NextToken")
                return self._to_pages(blocks, set(page_numbers))
            if status in ("FAILED", "PARTIAL_SUCCESS"):
                raise RuntimeError(f"Textract job {job_id} ended with {status}: {resp.get('StatusMessage')}")
            self._sleep(self.poll_seconds)
        raise TimeoutError(f"Textract job {job_id} did not finish")

    @staticmethod
    def _to_pages(blocks: List[dict], wanted: set) -> Dict:
        lines: Dict[int, List[dict]] = {}
        tables = 0
        for b in blocks:
            page = b.get("Page", 1)
            if b["BlockType"] == "LINE" and page in wanted:
                lines.setdefault(page, []).append(b)
            elif b["BlockType"] == "TABLE" and page in wanted:
                tables += 1
        pages = []
        for page in sorted(wanted):
            ls = lines.get(page, [])
            text = "\n".join(l.get("Text", "") for l in ls)
            conf = (sum(l.get("Confidence", 0.0) for l in ls) / len(ls) / 100.0) if ls else 0.0
            pages.append(PageText(page_number=page, text=text, method="Textract", confidence=round(conf, 4)))
        return {"pages": pages, "tables": tables}


def tesseract_ocr(pdf_bytes: bytes, page_numbers: List[int]) -> List[PageText]:
    """Local Tesseract fallback. Imports are lazy: the binary is optional."""
    import pypdfium2 as pdfium
    import pytesseract

    pdf = pdfium.PdfDocument(pdf_bytes)
    out = []
    for n in page_numbers:
        image = pdf[n - 1].render(scale=300 / 72).to_pil()
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        words = [(w, float(c)) for w, c in zip(data["text"], data["conf"]) if w.strip() and float(c) >= 0]
        text = pytesseract.image_to_string(image)
        conf = (sum(c for _, c in words) / len(words) / 100.0) if words else 0.0
        out.append(PageText(page_number=n, text=text, method="Tesseract", confidence=round(conf, 4)))
    return out


class TextExtractor:
    def __init__(
        self,
        config: PipelineConfig = None,
        textract: Optional[TextractOCR] = None,
        tesseract_fn: OcrFn = tesseract_ocr,
    ):
        self.config = config or PipelineConfig()
        self.textract = textract
        self.tesseract_fn = tesseract_fn

    # -- digital -----------------------------------------------------------
    def _extract_digital(self, pdf_bytes: bytes, page_numbers: List[int]) -> List[PageText]:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return [
            PageText(page_number=n, text=(reader.pages[n - 1].extract_text() or "").strip(),
                     method="PyPDF2", confidence=1.0)
            for n in page_numbers
        ]

    # -- scanned -----------------------------------------------------------
    def _extract_scanned(self, pdf_bytes: bytes, page_numbers: List[int], s3_uri: Optional[Dict]):
        """Returns (pages, table_count). Textract first, Tesseract on failure."""
        if self.textract and s3_uri:
            try:
                res = self.textract.ocr(s3_uri["bucket"], s3_uri["key"], page_numbers)
                return res["pages"], res["tables"]
            except Exception as e:
                logger.warning(f"Textract failed ({e}); falling back to Tesseract")
        try:
            return self.tesseract_fn(pdf_bytes, page_numbers), 0
        except Exception as e:
            raise PipelineFailure("ocr", f"All OCR engines failed: {e}")

    def extract(self, pdf_bytes: bytes, meta: DocumentMetadata, s3_uri: Optional[Dict] = None) -> ExtractedText:
        pages: List[PageText] = []
        tables = 0
        if meta.digital_page_numbers:
            pages += self._extract_digital(pdf_bytes, meta.digital_page_numbers)
        if meta.scanned_page_numbers:
            ocr_pages, tables = self._extract_scanned(pdf_bytes, meta.scanned_page_numbers, s3_uri)
            if all(p.confidence < self.config.ocr_min_confidence for p in ocr_pages):
                raise PipelineFailure(
                    "ocr",
                    f"OCR confidence < {self.config.ocr_min_confidence} on all pages",
                    {"ocr_confidence_scores": [p.confidence for p in ocr_pages]},
                )
            pages += ocr_pages

        # Noise filtering: drop blank / signature-only pages
        pages = [p for p in pages if len(p.text.strip()) >= self.config.min_page_chars_after_ocr]
        if not pages:
            raise PipelineFailure("text_extraction", "No usable text on any page")
        pages.sort(key=lambda p: p.page_number)

        ocr_scores = [p.confidence for p in pages if p.method != "PyPDF2"]
        methods = sorted({p.method for p in pages})
        method = methods[0] if len(methods) == 1 else "Mixed(" + "+".join(methods) + ")"
        return ExtractedText(
            pages=pages,
            extraction_method=method,
            confidence_score=round(sum(ocr_scores) / len(ocr_scores), 4) if ocr_scores else 1.0,
            detected_language=meta.detected_language
            if meta.detected_language != "unknown"
            else detect_language(" ".join(p.text for p in pages[:3])),
            table_count=tables,
        )
