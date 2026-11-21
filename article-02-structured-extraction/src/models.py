"""Pydantic data contracts shared across the pipeline stages."""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class PageKind(str, Enum):
    DIGITAL = "digital"
    SCANNED = "scanned"


class DocumentMetadata(BaseModel):
    """Output of Stage 1 (format detection)."""
    document_id: str
    total_pages: int
    pages_digital: int
    pages_scanned: int
    digital_page_numbers: List[int] = []
    scanned_page_numbers: List[int] = []
    detected_language: str = "unknown"
    file_size: int
    detected_text_density: float = Field(ge=0.0, le=1.0)

    @property
    def is_hybrid(self) -> bool:
        return self.pages_digital > 0 and self.pages_scanned > 0

    @property
    def doc_type(self) -> str:
        if self.is_hybrid:
            return "hybrid"
        return "digital" if self.pages_scanned == 0 else "scanned"


class PageText(BaseModel):
    page_number: int  # 1-indexed
    text: str
    method: str       # PyPDF2 | Textract | Tesseract
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class ExtractedText(BaseModel):
    """Output of Stage 2 (text extraction)."""
    pages: List[PageText]
    extraction_method: str  # PyPDF2 | Textract | Tesseract | Mixed(...)
    confidence_score: float
    detected_language: str = "unknown"
    table_count: int = 0

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"[[PAGE {p.page_number}]]\n{p.text}" for p in self.pages)


class Chunk(BaseModel):
    """Output of Stage 3 (chunking)."""
    chunk_id: int
    title: str
    text: str
    start_page: int
    end_page: int


class ClauseExtraction(BaseModel):
    """Per-clause contract with the AI: value, confidence, page, exact span."""
    value: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    page_number: Optional[int] = Field(default=None, ge=1)
    source_span: Optional[str] = None

    @property
    def found(self) -> bool:
        return bool(self.value and self.value.strip())


class ContractExtraction(BaseModel):
    """Row-level contract with Snowflake (see sql/ for the DDL).

    governing_law was added in extraction_version 2.0; it is Optional so
    v1 rows (NULL) and v2 rows coexist.
    """
    contract_id: str
    extraction_version: float
    contract_date: Optional[str] = None
    payment_terms: Optional[str] = None
    termination_conditions: Optional[str] = None
    liability_cap: Optional[str] = None
    governing_law: Optional[str] = None  # New field (v2.0)
    confidence_scores: Dict[str, float] = {}
    page_references: Dict[str, Optional[int]] = {}
    source_spans: Dict[str, Optional[str]] = {}
    # Lineage
    extraction_method: str = ""
    ocr_confidence: Optional[float] = None
    prompt_version: str = ""
    model_name: str = ""
    processing_timestamp: str = ""
    # Routing
    routing: str = "production"       # production | production_flagged | quarantine
    review_required: bool = False
    validation_flags: List[str] = []


class Route(str, Enum):
    PRODUCTION = "production"                  # >= 0.95
    PRODUCTION_FLAGGED = "production_flagged"  # 0.75 - 0.95
    QUARANTINE = "quarantine"                  # 0.5 - 0.75 (and any hallucination flag)
    DLQ = "dlq"                                # confidence 0 / hard failure


class PipelineFailure(Exception):
    """Hard failure at a named stage -> dead-letter queue."""

    def __init__(self, stage: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.details = details or {}
