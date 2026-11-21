"""Data contracts for events, rule scores and AI enrichments."""

from typing import Optional
from pydantic import BaseModel, Field


class Event(BaseModel):
    event_id: str
    user_id: str
    amount: float = Field(ge=0)
    merchant: str = "unknown"
    merchant_category: str = "general"
    country: str = "US"
    event_ts: float  # epoch seconds, event time
    event_type: str = "transaction"


def risk_band(score: int) -> str:
    """0-30 low, 31-70 medium, 71-100 high (article)."""
    if score <= 30:
        return "low"
    if score <= 70:
        return "medium"
    return "high"


class RuleScore(BaseModel):
    event_id: str
    user_id: str
    score: int = Field(ge=0, le=100)
    band: str
    rules_fired: list = []
    context_source: str = "redis"  # redis | local_cache | defaults
    latency_ms: float = 0.0


class Enrichment(BaseModel):
    """Validated per-event output from Claude."""
    event_id: str
    risk_score: int = Field(ge=0, le=100)
    anomaly_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


class EnrichmentRecord(BaseModel):
    """Row persisted to PostgreSQL, keyed on (event_id, enrichment_version)."""
    event_id: str
    enrichment_version: int
    user_id: str
    risk_score: int
    anomaly_type: str
    confidence: float
    reasoning: str = ""
    stale_context: bool = False  # produced while lag exceeded the quality threshold
    enriched_at: float = 0.0
