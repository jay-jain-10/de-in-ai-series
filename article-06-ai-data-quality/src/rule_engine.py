"""
Layer 1: deterministic syntactic rule engine.

Driven by a versioned YAML/JSON rule file (deployed like code). Applied to 100%
of rows. Same input always produces the same output (determinism/auditability).

Supported checks:
    NOT NULL
    UNIQUE
    BETWEEN <lo> AND <hi>
    REGEX <pattern>
    DATE NOT FUTURE
    REFERENCES <table>.<column>
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger(__name__)

SEVERITIES = ("critical", "high", "medium", "low")

_BETWEEN_RE = re.compile(r"^BETWEEN\s+(-?[\d.]+)\s+AND\s+(-?[\d.]+)$", re.IGNORECASE)
_REF_RE = re.compile(r"^REFERENCES\s+(\w+)\.(\w+)$", re.IGNORECASE)


class RuleConfigError(ValueError):
    """Raised when a rule file is malformed."""


@dataclass
class Rule:
    rule_id: str
    check: str
    column: str
    severity: str = "high"
    allow_null: bool = False


@dataclass
class RuleSet:
    source: str
    version: str
    domain: str
    rules: List[Rule]
    layer_2_examples: List[dict] = field(default_factory=list)
    layer_2: dict = field(default_factory=dict)
    criticality: str = "medium"


@dataclass
class RuleViolation:
    record_id: str
    rule_id: str
    check: str
    column: str
    severity: str
    detail: str


def load_ruleset(path: str) -> RuleSet:
    """Load and validate a YAML or JSON rule file."""
    with open(path, "r") as f:
        text = f.read()
    data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    return parse_ruleset(data)


def parse_ruleset(data: dict) -> RuleSet:
    for key in ("source", "version", "layer_1_rules"):
        if key not in data:
            raise RuleConfigError(f"Rule file missing required key: {key}")
    rules, seen = [], set()
    for raw in data["layer_1_rules"]:
        for key in ("rule_id", "check", "column"):
            if key not in raw:
                raise RuleConfigError(f"Rule missing '{key}': {raw}")
        if raw["rule_id"] in seen:
            raise RuleConfigError(f"Duplicate rule_id: {raw['rule_id']}")
        seen.add(raw["rule_id"])
        sev = raw.get("severity", "high")
        if sev not in SEVERITIES:
            raise RuleConfigError(f"Invalid severity '{sev}' on {raw['rule_id']}")
        _compile_check(raw["check"])  # fail fast on unknown/invalid checks
        rules.append(
            Rule(raw["rule_id"], raw["check"], raw["column"], sev, bool(raw.get("allow_null", False)))
        )
    return RuleSet(
        source=data["source"],
        version=str(data["version"]),
        domain=data.get("domain", "healthcare"),
        rules=rules,
        layer_2_examples=data.get("layer_2_examples", []),
        layer_2=data.get("layer_2", {}),
        criticality=data.get("criticality", "medium"),
    )


def _compile_check(check: str):
    c = check.strip()
    up = c.upper()
    if up == "NOT NULL":
        return ("not_null", None)
    if up == "UNIQUE":
        return ("unique", None)
    if up == "DATE NOT FUTURE":
        return ("date_not_future", None)
    m = _BETWEEN_RE.match(c)
    if m:
        return ("between", (float(m.group(1)), float(m.group(2))))
    if up.startswith("REGEX "):
        try:
            return ("regex", re.compile(c[6:].strip()))
        except re.error as e:
            raise RuleConfigError(f"Invalid regex in '{check}': {e}")
    m = _REF_RE.match(c)
    if m:
        return ("references", (m.group(1), m.group(2)))
    raise RuleConfigError(f"Unsupported check: {check}")


def _is_null(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def record_key(row: dict, id_column: str) -> str:
    """Record id used in violations / quarantine; null ids map to '<null>'."""
    v = row.get(id_column)
    return "<null>" if _is_null(v) else str(v)


def _parse_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class RuleEngine:
    """Applies a RuleSet to every row; returns (passed, failed, violations)."""

    def __init__(
        self,
        ruleset: RuleSet,
        reference_data: Optional[Dict[str, Set[Any]]] = None,
        id_column: str = "claim_id",
        today: Optional[date] = None,
    ):
        self.ruleset = ruleset
        self.reference_data = reference_data or {}
        self.id_column = id_column
        self.today = today or date.today()
        self._compiled = [(r, _compile_check(r.check)) for r in ruleset.rules]

    def validate(
        self, rows: Iterable[dict]
    ) -> Tuple[List[dict], List[dict], List[RuleViolation]]:
        rows = list(rows)
        dup_values = self._find_duplicates(rows)
        passed, failed, violations = [], [], []
        for row in rows:
            rid = record_key(row, self.id_column)
            row_violations = []
            for rule, (kind, arg) in self._compiled:
                detail = self._evaluate(rule, kind, arg, row, dup_values)
                if detail:
                    row_violations.append(
                        RuleViolation(rid, rule.rule_id, rule.check, rule.column, rule.severity, detail)
                    )
            if row_violations:
                failed.append(row)
                violations.extend(row_violations)
            else:
                passed.append(row)
        logger.info(
            f"Layer 1: {len(rows)} rows, {len(passed)} passed, {len(failed)} failed"
        )
        return passed, failed, violations

    def _find_duplicates(self, rows: List[dict]) -> Dict[str, Set[Any]]:
        dups = {}
        for rule, (kind, _) in self._compiled:
            if kind != "unique":
                continue
            seen, dup = set(), set()
            for row in rows:
                v = row.get(rule.column)
                if _is_null(v):
                    continue
                (dup if v in seen else seen).add(v)
            dups[rule.rule_id] = dup
        return dups

    def _evaluate(self, rule, kind, arg, row, dup_values) -> Optional[str]:
        v = row.get(rule.column)
        if kind == "not_null":
            return f"{rule.column} is null" if _is_null(v) else None
        if _is_null(v):
            # Null handling is the job of NOT NULL rules; other rules skip nulls
            # only when explicitly allowed, otherwise a null is a failure.
            return None if rule.allow_null else f"{rule.column} is null"
        if kind == "unique":
            return f"duplicate {rule.column}={v}" if v in dup_values.get(rule.rule_id, set()) else None
        if kind == "between":
            try:
                num = float(v)
            except (TypeError, ValueError):
                return f"{rule.column}={v!r} is not numeric"
            lo, hi = arg
            return None if lo <= num <= hi else f"{rule.column}={num} outside [{lo}, {hi}]"
        if kind == "regex":
            return None if arg.fullmatch(str(v)) else f"{rule.column}={v!r} fails pattern"
        if kind == "date_not_future":
            d = _parse_date(v)
            if d is None:
                return f"{rule.column}={v!r} is not a valid date"
            return f"{rule.column}={d} is in the future" if d > self.today else None
        if kind == "references":
            table, col = arg
            ref = self.reference_data.get(f"{table}.{col}")
            if ref is None:
                return f"reference data {table}.{col} not provided"
            return None if v in ref else f"{rule.column}={v!r} not found in {table}.{col}"
        return f"unhandled check kind {kind}"
