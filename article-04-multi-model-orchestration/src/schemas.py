"""Data contracts between models, plus the TaskResult record (Article 4: data contracts)."""

from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

TASKS = ("sentiment", "aspect_extraction", "toxicity_detection")


class ContractViolation(Exception):
    """Raised when a model output (or upstream result) breaks its schema."""


class SentimentOutput(BaseModel):
    sentiment: Literal["positive", "negative", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)


class AspectOutput(BaseModel):
    aspects: list[str]
    unsafe_language: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)


class ToxicityOutput(BaseModel):
    toxic: bool
    categories: list[str] = []
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)


TASK_SCHEMAS = {
    "sentiment": SentimentOutput,
    "aspect_extraction": AspectOutput,
    "toxicity_detection": ToxicityOutput,
}

MAX_REVIEW_CHARS = 5000


def validate_task_output(task: str, payload: dict) -> dict:
    """Validate a model output against its contract; raise ContractViolation."""
    schema = TASK_SCHEMAS[task]
    if not isinstance(payload, dict):
        raise ContractViolation(f"{task}: output is not an object")
    for field_name in schema.model_fields:
        if schema.model_fields[field_name].is_required() and field_name not in payload:
            raise ContractViolation(f"{task}: Missing field: {field_name}")
    try:
        return schema(**payload).model_dump()
    except ValidationError as e:
        raise ContractViolation(f"{task}: {e.errors()[0]['msg']} ({e.errors()[0]['loc']})") from e


def validate_review_input(text) -> str:
    """Input contract: non-empty UTF-8 text, max 5000 chars."""
    if not isinstance(text, str) or not text.strip():
        raise ContractViolation("review text is empty")
    text.encode("utf-8")
    return text[:MAX_REVIEW_CHARS]


def fallback_payload(task: str) -> dict:
    """Degraded-but-explicit payload used when a model cannot answer."""
    if task == "sentiment":
        return {"sentiment": "unknown", "confidence": 0.0, "needs_review": True}
    if task == "aspect_extraction":
        return {"aspects": [], "unsafe_language": False, "confidence": 0.0, "needs_review": True}
    return {"toxic": None, "categories": [], "confidence": 0.0, "needs_review": True}


@dataclass
class TaskResult:
    review_id: str
    task: str
    model: str                       # alias (haiku, sonnet_with_rag, ...)
    model_version: str               # concrete model id
    result: dict
    confidence: float = 0.0
    fallback: bool = False
    failure_reason: Optional[str] = None
    cache_hit: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    model_decision: str = ""
    parent_task: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
