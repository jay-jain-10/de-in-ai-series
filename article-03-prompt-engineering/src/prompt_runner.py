"""Run a registry prompt against Claude and enforce its output contract.

If the output violates the contract, retry once with a clearer example of the
required shape (Implementation Checklist item 2). If it still fails, the
result is returned with ``schema_valid=False`` so callers can count it toward
schema-compliance metrics instead of silently loading bad data.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import anthropic
from pydantic import BaseModel

from .output_contracts import ContractViolation, parse_output
from .prompt_registry import PromptRegistry

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    prompt_name: str
    prompt_version: str
    model: str
    input_text: str
    output: Optional[BaseModel] = None
    schema_valid: bool = False
    raw_text: str = ""
    attempts: int = 0
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def api_failed(self) -> bool:
        """True if no model response was ever received (outage, not prompt quality)."""
        return self.output is None and not self.raw_text and self.error.startswith("API error")

    @property
    def confidence(self) -> Optional[float]:
        return getattr(self.output, "confidence", None) if self.output is not None else None

    def comparable(self) -> Optional[dict]:
        """Output minus confidence; used for divergence comparisons."""
        if self.output is None:
            return None
        d = self.output.model_dump()
        d.pop("confidence", None)
        return d


class PromptRunner:
    def __init__(self, client, registry: PromptRegistry, max_retries: int = 2):
        self.client = client
        self.registry = registry
        self.max_retries = max_retries

    def run(self, prompt_name: str, input_text: str, version: Optional[str] = None) -> RunResult:
        spec = self.registry.get(prompt_name, version)
        result = RunResult(spec.name, spec.version, spec.model, input_text)
        messages = [{"role": "user", "content": input_text}]

        for attempt in range(1, self.max_retries + 1):
            result.attempts = attempt
            try:
                response = self.client.messages.create(
                    model=spec.model,
                    max_tokens=spec.max_tokens,
                    system=spec.system_prompt,
                    messages=messages,
                )
            except anthropic.APIError as e:
                result.error = f"API error: {e}"
                logger.error("API error for %s v%s: %s", spec.name, spec.version, e)
                continue

            result.input_tokens += response.usage.input_tokens
            result.output_tokens += response.usage.output_tokens
            raw = response.content[0].text
            result.raw_text = raw
            try:
                result.output = parse_output(spec.contract, raw)
                result.schema_valid = True
                result.error = ""
                return result
            except ContractViolation as e:
                result.error = f"Contract violation: {e}"
                logger.warning("%s v%s attempt %d violated contract", spec.name, spec.version, attempt)
                messages = [
                    {"role": "user", "content": input_text},
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Your previous output did not match the required JSON schema. "
                            "Return ONLY a JSON object matching exactly this schema, with no extra fields:\n"
                            + json.dumps(spec.validation_schema)
                        ),
                    },
                ]
        return result
