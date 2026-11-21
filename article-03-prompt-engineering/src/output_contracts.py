"""Pydantic output contracts for prompts.

The Pydantic schema IS the data contract between the AI stage and the
warehouse. ``extra="forbid"`` means a prompt edit that makes Claude return
``vendor_company`` instead of ``vendor_name`` fails validation here rather
than turning into NULLs in dbt.
"""

import json
from typing import Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ContractViolation(Exception):
    """Raised when model output does not satisfy a prompt's contract."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SentimentOutput(_Contract):
    sentiment: Literal["positive", "neutral", "negative", "frustrated"]
    confidence: float = Field(ge=0.0, le=1.0)
    # Additional field added in v1.5 of the contract (optional => backwards compatible)
    subsentiment: Optional[Literal["angry", "disappointed"]] = None


class EscalationOutput(_Contract):
    escalation_risk: float = Field(ge=0.0, le=1.0)
    urgency: Literal["low", "medium", "high", "critical"]
    confidence: float = Field(ge=0.0, le=1.0)


class VendorExtraction(_Contract):
    vendor_name: str = Field(min_length=1)
    terms_days: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)


CONTRACTS: dict = {
    "SentimentOutput": SentimentOutput,
    "EscalationOutput": EscalationOutput,
    "VendorExtraction": VendorExtraction,
}

# Fields whose string values must literally appear in the source text.
GROUNDED_FIELDS: dict = {
    "SentimentOutput": [],
    "EscalationOutput": [],
    "VendorExtraction": ["vendor_name"],
}


def get_contract(name: str) -> Type[BaseModel]:
    try:
        return CONTRACTS[name]
    except KeyError:
        raise ContractViolation(f"Unknown output contract: {name}")


def strip_fences(raw_text: str) -> str:
    """Handle markdown-wrapped JSON (same behaviour as Article 1)."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return raw_text


def parse_output(contract_name: str, raw_text: str) -> BaseModel:
    """Parse + validate raw model text against the named contract."""
    model_cls = get_contract(contract_name)
    try:
        payload = json.loads(strip_fences(raw_text))
    except (json.JSONDecodeError, IndexError) as e:
        raise ContractViolation(f"Invalid JSON: {e}")
    if not isinstance(payload, dict):
        raise ContractViolation("Output must be a JSON object")
    try:
        return model_cls(**payload)
    except ValidationError as e:
        raise ContractViolation(str(e))


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


def find_ungrounded(output: BaseModel, source_text: str, fields: list) -> list:
    """Return grounded fields whose extracted value is NOT in the source text."""
    src = _norm(source_text)
    bad = []
    for f in fields:
        value = getattr(output, f, None)
        if value is None:
            continue
        if _norm(value) not in src:
            bad.append(f)
    return bad
