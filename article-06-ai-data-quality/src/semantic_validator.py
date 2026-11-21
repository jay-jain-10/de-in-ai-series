"""
Layer 2: semantic validation with Claude Sonnet + few-shot domain examples.

Output contract: {is_consistent, anomaly_type, confidence, reasoning}.
A record is flagged only if is_consistent=false AND confidence >= domain
threshold (healthcare 0.60, financial 0.70, marketing 0.80).
"""

import json
import logging
import time
from typing import Dict, List, Optional

import anthropic
from pydantic import BaseModel, Field

from src.config import CONFIDENCE_THRESHOLDS

logger = logging.getLogger(__name__)

# Fields sent to the model (never free-form PHI in this synthetic demo).
CONTEXT_FIELDS = [
    "diagnosis_code", "diagnosis_description", "procedure_code",
    "procedure_description", "medication", "dosage_mg", "patient_age",
    "admit_date", "discharge_date", "claim_date", "claim_amount", "care_unit",
]


class SemanticResult(BaseModel):
    """Data contract for the Layer 2 AI output."""
    is_consistent: bool
    anomaly_type: str = "none"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""


SEMANTIC_PROMPT = """You are a data quality expert in {domain}. Decide whether the record below is logically consistent given industry standards (diagnosis vs procedure compatibility, physiologically possible medication dosages, valid temporal sequences).

Examples of reviewed records:
{examples}

Return a JSON object with exactly these fields:
- is_consistent: boolean
- anomaly_type: short snake_case label, or "none" if consistent
- confidence: float 0.0-1.0 (your confidence that the record is anomalous when is_consistent is false)
- reasoning: brief explanation (max 60 words)

Record to evaluate:
{record}

Return ONLY valid JSON. No markdown, no explanation outside the JSON."""


def format_examples(examples: List[dict]) -> str:
    lines = []
    for i, ex in enumerate(examples, 1):
        out = {
            "is_consistent": ex["is_consistent"],
            "anomaly_type": ex.get("anomaly_type", "none"),
        }
        lines.append(
            f"Example {i} ({ex.get('description', '')}):\n"
            f"Record: {json.dumps(ex['example'])}\nOutput: {json.dumps(out)}"
        )
    return "\n\n".join(lines)


class SemanticValidator:
    """Claude-backed semantic validator with retry, schema validation and
    graceful degradation (validation_error set; record is NOT silently passed
    as verified)."""

    def __init__(
        self,
        api_key: str,
        examples: List[dict],
        domain: str = "healthcare",
        model: str = "claude-sonnet-5-5",
        confidence_threshold: Optional[float] = None,
    ):
        if not 3 <= len(examples) <= 5:
            logger.warning(f"Expected 3-5 few-shot examples, got {len(examples)}")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.domain = domain
        self.examples = examples
        self.confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else CONFIDENCE_THRESHOLDS.get(domain, 0.70)
        )
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._request_count = 0      # API attempts (includes retries)
        self._record_count = 0       # records passed to validate_record
        self._failure_count = 0      # records that ended with validation_error

    def build_prompt(self, record: dict) -> str:
        ctx = {k: record.get(k) for k in CONTEXT_FIELDS if k in record}
        return SEMANTIC_PROMPT.format(
            domain=self.domain,
            examples=format_examples(self.examples),
            record=json.dumps(ctx, default=str),
        )

    def validate_record(self, record: dict, max_retries: int = 3) -> dict:
        """Returns dict: {result: SemanticResult|None, flagged: bool, validation_error: str|None}."""
        prompt = self.build_prompt(record)
        self._record_count += 1
        last_error = "unknown"
        for attempt in range(max_retries):
            try:
                self._request_count += 1
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                self.total_input_tokens += response.usage.input_tokens
                self.total_output_tokens += response.usage.output_tokens
                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                result = SemanticResult(**json.loads(raw))
                return {
                    "result": result,
                    "flagged": self.should_flag(result),
                    "validation_error": None,
                }
            except (json.JSONDecodeError, ValueError, TypeError, KeyError,
                    IndexError, AttributeError) as e:
                # pydantic ValidationError subclasses ValueError; IndexError /
                # AttributeError cover empty or non-text content blocks.
                last_error = f"invalid output: {e}"
                logger.warning(f"Attempt {attempt + 1}/{max_retries} bad output: {e}")
            except anthropic.RateLimitError:
                last_error = "rate limit exceeded"
                time.sleep(2 ** (attempt + 1))
            except anthropic.APIError as e:
                last_error = f"API error: {e}"
                logger.error(last_error)
        self._failure_count += 1
        return {"result": None, "flagged": False, "validation_error": last_error}

    def should_flag(self, result: SemanticResult) -> bool:
        return (not result.is_consistent) and result.confidence >= self.confidence_threshold

    def validate_batch(self, records: List[dict], id_column: str = "claim_id") -> Dict[str, dict]:
        return {str(r[id_column]): self.validate_record(r) for r in records}

    def get_cost_estimate(self) -> dict:
        """Sonnet pricing: $3.00 / 1M input tokens, $15.00 / 1M output tokens."""
        cost = (self.total_input_tokens / 1e6) * 3.0 + (self.total_output_tokens / 1e6) * 15.0
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_requests": self._request_count,
            "total_records": self._record_count,
            "total_failures": self._failure_count,
            # failed records / records attempted (not / API attempts, which
            # would understate the rate whenever retries happen)
            "failure_rate": round(self._failure_count / max(self._record_count, 1), 4),
            "estimated_cost_usd": round(cost, 4),
        }
