"""Two-tier validation shared by every pipeline.

Layer 1 (sync, deterministic, cheap): required fields, types, ranges, patterns, confidence floor,
  plus domain rules registered by business teams (foreign-key integrity, amount logic, ...).
Layer 2 (sampled, semantic): Claude Sonnet via gateway /validate - contradictions, implausible
  values, domain inconsistency.
Both layers produce ValidationResult objects; failures go to quarantine with reasons.
"""

import hashlib
import re
from datetime import date, datetime
from typing import Any, Callable, Optional

from .contracts import ExtractedRecord, ValidationIssue, ValidationResult

# ---- Field specs (declared in pipeline YAML) --------------------------------------

from pydantic import BaseModel, Field


class FieldSpec(BaseModel):
    type: str = "string"            # string | number | integer | date | boolean | list
    required: bool = False
    description: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    pattern: Optional[str] = None


# ---- Domain rule registry ----------------------------------------------------------

RuleFn = Callable[[dict, dict], list]    # (typed fields, context) -> list of failure messages
RULES: dict[str, RuleFn] = {}


def register_rule(name: str):
    """Business teams register domain rules here, e.g. @register_rule('claims.amount_logic')."""
    def deco(fn: RuleFn) -> RuleFn:
        RULES[name] = fn
        return fn
    return deco


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def coerce(spec: FieldSpec, value: Any) -> Any:
    """Coerce to the declared type or raise ValueError. Returns JSON-friendly values."""
    t = spec.type
    if t == "string":
        if isinstance(value, (dict, list)):
            raise ValueError("expected string")
        return str(value).strip()
    if t in ("number", "integer"):
        if isinstance(value, bool):
            raise ValueError("expected number")
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        num = float(value)
        if t == "integer":
            if num != int(num):
                raise ValueError("expected integer")
            return int(num)
        return num
    if t == "date":
        if isinstance(value, (date, datetime)):
            return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
        return date.fromisoformat(str(value).strip()).isoformat()
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValueError("expected boolean")
    if t == "list":
        if not isinstance(value, list):
            raise ValueError("expected list")
        return value
    raise ValueError(f"unknown type '{t}'")


class Layer1Validator:
    def __init__(self, fields: dict[str, FieldSpec], rule_names: list[str],
                 min_confidence: float = 0.7, context: Optional[dict] = None):
        unknown = [r for r in rule_names if r not in RULES]
        if unknown:
            raise ValueError(f"unknown validation rules: {unknown}")
        self.fields, self.rule_names, self.min_confidence = fields, rule_names, min_confidence
        self.context = context or {}

    def validate(self, record: ExtractedRecord) -> tuple[ValidationResult, dict]:
        """Returns (result, typed_fields). Typed fields are what downstream stages should use."""
        issues: list[ValidationIssue] = []
        add = lambda rule, msg: issues.append(ValidationIssue(layer=1, rule=rule, message=msg))
        typed: dict[str, Any] = {}

        for name, spec in self.fields.items():
            raw = record.fields.get(name)
            if _is_blank(raw):
                typed[name] = None
                if spec.required:
                    add("required_fields", f"{name} is required but missing")
                continue
            try:
                value = coerce(spec, raw)
            except (ValueError, TypeError):
                typed[name] = raw
                add("types", f"{name} must be {spec.type}, got {raw!r}")
                continue
            typed[name] = value
            if spec.min is not None and isinstance(value, (int, float)) and value < spec.min:
                add("ranges", f"{name}={value} is below minimum {spec.min}")
            if spec.max is not None and isinstance(value, (int, float)) and value > spec.max:
                add("ranges", f"{name}={value} is above maximum {spec.max}")
            if spec.pattern and isinstance(value, str) and not re.fullmatch(spec.pattern, value):
                add("patterns", f"{name}={value!r} does not match {spec.pattern}")

        if record.extraction_confidence < self.min_confidence:
            add("min_confidence",
                f"extraction_confidence {record.extraction_confidence:.2f} < {self.min_confidence}")

        ctx = {**self.context, "extraction_confidence": record.extraction_confidence}
        clean_for_rules = not any(i.rule in ("types",) for i in issues)
        if clean_for_rules:   # domain rules assume typed values
            for rule in self.rule_names:
                for msg in RULES[rule](typed, ctx):
                    add(rule, msg)

        return ValidationResult(document_id=record.document_id, layer=1, passed=not issues,
                                issues=issues), typed


class Layer2Validator:
    """Sampled semantic validation through the gateway (/validate -> Sonnet)."""

    def __init__(self, gateway, rules: list[str], sample_rate: float = 0.1,
                 always_sample_risks: Optional[list[str]] = None):
        self.gateway, self.rules, self.sample_rate = gateway, rules, sample_rate
        self.always = set(always_sample_risks or [])

    def should_sample(self, document_id: str, risk_class: Optional[str] = None) -> bool:
        """Deterministic: the same document is always (or never) sampled => re-runs are idempotent."""
        if risk_class in self.always:
            return True
        h = int(hashlib.sha256(document_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return h < self.sample_rate

    def validate(self, record: ExtractedRecord, typed_fields: dict, task_type: str = "validate_semantic"
                 ) -> tuple[ValidationResult, Any]:
        resp = self.gateway.validate({**typed_fields}, self.rules, task_type=task_type)
        issues = [ValidationIssue(layer=2, rule="semantic", message=m) for m in resp.result["issues"]]
        passed = bool(resp.result["valid"]) and not issues
        if not resp.result["valid"] and not issues:
            issues.append(ValidationIssue(layer=2, rule="semantic", message="validator flagged record invalid"))
        # A degraded gateway (breaker open) returns a default 'valid' with confidence 0 -> treat as unverified.
        return ValidationResult(document_id=record.document_id, layer=2, passed=passed, issues=issues,
                                sampled=True, confidence=resp.result.get("confidence")), resp
