"""Pydantic data contracts enforced at every platform boundary.

    ingestion --(IncomingDocument)--> processing --(TextChunk, <4000 tokens)-->
    AI gateway --(Gateway*Response, valid JSON)--> validation
    --(ValidatedRecord: only L1+L2 clean)--> warehouse
    failures --(QuarantineRecord)--> human review

Contracts are versioned (CONTRACT_VERSION). When the extraction schema evolves, bump
the version and make downstream consumers handle both explicitly.
"""

import base64
import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

CONTRACT_VERSION = "v1"
MAX_CHUNK_TOKENS = 4000


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Swap for a real tokenizer in prod."""
    return (len(text) + 3) // 4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Ingestion -> Processing ---------------------------------------------------

class SourceType(str, Enum):
    EMAIL = "email"
    PORTAL = "portal"
    FAX = "fax"
    API = "api"
    DOCUSIGN = "docusign"


class IncomingDocument(BaseModel):
    """Every document must arrive with source_type, source_id and timestamp."""
    contract_version: str = CONTRACT_VERSION
    source_type: SourceType
    source_id: str = Field(min_length=1)
    timestamp: datetime
    team: str = Field(min_length=1)
    pipeline: str = Field(min_length=1)
    mime_type: str = "text/plain"
    content_b64: str = Field(min_length=1)

    @classmethod
    def from_bytes(cls, data: bytes, **kwargs) -> "IncomingDocument":
        return cls(content_b64=base64.b64encode(data).decode("ascii"), **kwargs)

    @classmethod
    def from_text(cls, text: str, **kwargs) -> "IncomingDocument":
        return cls.from_bytes(text.encode("utf-8"), **kwargs)

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.content_b64)

    @property
    def document_size(self) -> int:
        return len(self.content)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class DocumentEnvelope(BaseModel):
    """What actually lands on Kafka topic documents.incoming (metadata + payload)."""
    document_id: str
    source_type: SourceType
    source_id: str
    document_size: int
    timestamp: datetime
    team: str
    pipeline: str
    mime_type: str
    content_b64: str
    contract_version: str = CONTRACT_VERSION


# --- Processing -> Gateway -------------------------------------------------------

class TextChunk(BaseModel):
    """Chunks passed to the AI gateway must be < 4000 tokens and keep doc boundaries."""
    document_id: str
    chunk_index: int = Field(ge=0)
    total_chunks: int = Field(ge=1)
    text: str
    token_estimate: int

    @model_validator(mode="after")
    def _check(self):
        if self.token_estimate >= MAX_CHUNK_TOKENS:
            raise ValueError(f"chunk has {self.token_estimate} tokens; must be < {MAX_CHUNK_TOKENS}")
        if self.chunk_index >= self.total_chunks:
            raise ValueError("chunk_index must be < total_chunks")
        return self


class ProcessedDocument(BaseModel):
    document_id: str
    text: str
    language: str = "en"
    format: str
    page_count: int = 1
    was_scanned: bool = False
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    failed_pages: list[int] = Field(default_factory=list)
    chunks: list[TextChunk]


# --- Gateway ---------------------------------------------------------------------

class TaskType(str, Enum):
    EXTRACT = "extract"
    CLASSIFY = "classify"
    SUMMARIZE = "summarize"
    VALIDATE = "validate"
    SCORE = "score"


class CallerMetadata(BaseModel):
    """Logged with every API call; the basis for cost attribution."""
    team: str
    pipeline: str
    task_type: str   # e.g. "extract_claim_facts"


class _GatewayRequest(BaseModel):
    caller: CallerMetadata


class ExtractRequest(_GatewayRequest):
    text: str = Field(min_length=1)
    extraction_schema: dict[str, str] = Field(min_length=1)  # field -> description


class ClassifyRequest(_GatewayRequest):
    text: str = Field(min_length=1)
    categories: list[str] = Field(min_length=2)


class SummarizeRequest(_GatewayRequest):
    text: str = Field(min_length=1)
    max_words: int = Field(default=100, ge=10, le=500)


class ValidateRequest(_GatewayRequest):
    record: dict[str, Any]
    rules: list[str] = Field(min_length=1)


class ScoreRequest(_GatewayRequest):
    record: dict[str, Any]
    scoring_logic: str = Field(min_length=1)
    complexity: Literal["low", "high"] = "low"


class GatewayResponse(BaseModel):
    request_id: str
    task: TaskType
    result: dict[str, Any]
    model: str
    cached: bool = False
    cache_tier: Optional[str] = None     # "L1" | "L2" | None
    degraded: bool = False               # breaker open / all attempts failed -> default
    prompt_version: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 0


# --- Validation -> Warehouse ----------------------------------------------------

class ExtractedRecord(BaseModel):
    document_id: str
    pipeline: str
    team: str
    schema_version: str
    fields: dict[str, Any]
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    extracted_at: datetime = Field(default_factory=utcnow)


class ValidationIssue(BaseModel):
    layer: Literal[1, 2]
    rule: str
    message: str


class ValidationResult(BaseModel):
    document_id: str
    layer: Literal[1, 2]
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    sampled: bool = True
    confidence: Optional[float] = None


class ValidatedRecord(BaseModel):
    """The only shape the warehouse loader accepts: passed L1 AND (L2 passed, not sampled, or human-reviewed)."""
    record: ExtractedRecord
    risk_class: str
    risk_confidence: float = Field(ge=0.0, le=1.0)
    fraud_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    layer1_passed: bool
    layer2_status: Literal["passed", "not_sampled", "human_reviewed"]
    processing_version: str

    @model_validator(mode="after")
    def _only_clean(self):
        if not self.layer1_passed:
            raise ValueError("records failing layer 1 must go to quarantine, not the warehouse")
        return self


class QuarantineStage(str, Enum):
    EXTRACTION = "extraction"
    LAYER1 = "layer1"
    LAYER2 = "layer2"


class QuarantineRecord(BaseModel):
    quarantine_id: str
    document_id: str
    pipeline: str
    team: str
    stage: QuarantineStage
    reasons: list[str] = Field(min_length=1)
    extracted: Optional[dict[str, Any]] = None
    extraction_confidence: Optional[float] = None
    risk_class: Optional[str] = None
    document_excerpt: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    status: Literal["pending", "resolved"] = "pending"


class ReviewDecisionType(str, Enum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    UPSTREAM = "upstream"   # problem is upstream (bad doc / bad source), not the extraction


class ReviewDecision(BaseModel):
    quarantine_id: str
    reviewer: str
    decision: ReviewDecisionType
    corrected_fields: Optional[dict[str, Any]] = None
    note: str = ""
    decided_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _correct_needs_fields(self):
        if self.decision == ReviewDecisionType.CORRECT and not self.corrected_fields:
            raise ValueError("decision=correct requires corrected_fields")
        return self


# --- Lineage / cost ---------------------------------------------------------------

class LineageStep(BaseModel):
    document_id: str
    step: str
    timestamp: datetime = Field(default_factory=utcnow)
    processing_version: str
    model_used: Optional[str] = None
    token_count: int = 0
    cost_usd: float = 0.0
    confidence_score: Optional[float] = None
    details: dict[str, Any] = Field(default_factory=dict)


class CostEvent(BaseModel):
    event_id: str
    called_at: datetime
    logged_at: datetime
    team: str
    pipeline: str
    task_type: str
    task: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool
    prompt_version: str = ""
