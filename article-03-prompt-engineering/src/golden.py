"""Golden-dataset regression testing and the CI gate decision.

A golden dataset is a list of cases (JSON or YAML) owned by the Product/ML
team. The same suite is run against the baseline and candidate prompt
versions; ``compare`` turns the two results into a merge decision:

  * BLOCK if > 5% of golden cases fail on the candidate
  * BLOCK if > 5% of cases regress (baseline passed, candidate fails)
  * BLOCK if average confidence drops by more than 0.05 vs baseline
  * green flag if the candidate passes > 5% more cases than baseline
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

from .config import GovernanceConfig
from .output_contracts import GROUNDED_FIELDS, find_ungrounded
from .prompt_registry import PromptRegistry
from .prompt_runner import PromptRunner, RunResult


@dataclass
class GoldenCase:
    name: str
    input: str
    expected: dict = field(default_factory=dict)            # exact-match fields
    expected_approx: dict = field(default_factory=dict)     # field -> [value, tolerance]
    min_confidence: Optional[float] = None
    acceptable_confidence_range: Optional[List[float]] = None
    grounded_fields: Optional[List[str]] = None             # override contract default
    note: str = ""


def load_golden(path: str) -> List[GoldenCase]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) if path.endswith((".yaml", ".yml")) else json.load(f)
    cases = data["test_cases"] if isinstance(data, dict) else data
    return [GoldenCase(**c) for c in cases]


@dataclass
class CaseResult:
    name: str
    passed: bool
    failures: List[str]
    confidence: Optional[float]
    schema_valid: bool


def evaluate_case(case: GoldenCase, result: RunResult, contract: str) -> CaseResult:
    failures: List[str] = []
    if not result.schema_valid:
        failures.append(f"schema: {result.error or 'no valid output'}")
        return CaseResult(case.name, False, failures, None, False)

    out = result.output.model_dump()
    for k, v in case.expected.items():
        if out.get(k) != v:
            failures.append(f"{k}: expected {v!r}, got {out.get(k)!r}")
    for k, (target, tol) in case.expected_approx.items():
        got = out.get(k)
        if not isinstance(got, (int, float)) or isinstance(got, bool) or abs(got - target) > tol:
            failures.append(f"{k}: expected {target}±{tol}, got {got}")

    conf = result.confidence
    if case.min_confidence is not None and (conf is None or conf < case.min_confidence):
        failures.append(f"confidence {conf} < min {case.min_confidence}")
    if case.acceptable_confidence_range is not None:
        lo, hi = case.acceptable_confidence_range
        if conf is None or not (lo <= conf <= hi):
            failures.append(f"confidence {conf} outside [{lo}, {hi}]")

    fields = case.grounded_fields if case.grounded_fields is not None else GROUNDED_FIELDS.get(contract, [])
    for f in find_ungrounded(result.output, case.input, fields):
        failures.append(f"hallucination: {f}={out.get(f)!r} not in source text")

    return CaseResult(case.name, not failures, failures, conf, True)


@dataclass
class SuiteResult:
    prompt_name: str
    version: str
    cases: List[CaseResult]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def failed(self) -> List[CaseResult]:
        return [c for c in self.cases if not c.passed]

    @property
    def fail_rate(self) -> float:
        return len(self.failed) / self.total if self.total else 0.0

    @property
    def passed_names(self) -> set:
        return {c.name for c in self.cases if c.passed}

    @property
    def avg_confidence(self) -> float:
        confs = [c.confidence for c in self.cases if c.confidence is not None]
        return sum(confs) / len(confs) if confs else 0.0


def run_suite(runner: PromptRunner, prompt_name: str, version: str, cases: List[GoldenCase]) -> SuiteResult:
    contract = runner.registry.get(prompt_name, version).contract
    results = []
    for case in cases:
        r = runner.run(prompt_name, case.input, version)
        results.append(evaluate_case(case, r, contract))
    return SuiteResult(prompt_name, version, results)


@dataclass
class GateDecision:
    prompt_name: str
    baseline_version: str
    candidate_version: str
    blocked: bool
    green_flag: bool
    reasons: List[str]
    fail_rate: float
    regression_rate: float
    improvement_rate: float
    confidence_drop: float
    regressions: List[str]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def compare(baseline: SuiteResult, candidate: SuiteResult, cfg: Optional[GovernanceConfig] = None) -> GateDecision:
    cfg = cfg or GovernanceConfig()
    total = candidate.total or 1
    regressions = sorted(baseline.passed_names - candidate.passed_names)
    improvements = sorted(candidate.passed_names - baseline.passed_names)
    regression_rate = len(regressions) / total
    improvement_rate = len(improvements) / total
    conf_drop = baseline.avg_confidence - candidate.avg_confidence

    reasons = []
    if candidate.fail_rate > cfg.max_golden_fail_rate:
        reasons.append(
            f"{candidate.fail_rate:.1%} of golden cases fail (limit {cfg.max_golden_fail_rate:.0%})"
        )
    if regression_rate > cfg.max_golden_fail_rate:
        reasons.append(
            f"{regression_rate:.1%} of cases regressed vs v{baseline.version}: {', '.join(regressions)}"
        )
    if conf_drop > cfg.max_confidence_drop:
        reasons.append(
            f"average confidence dropped {conf_drop:.3f} (limit {cfg.max_confidence_drop})"
        )
    blocked = bool(reasons)
    return GateDecision(
        prompt_name=candidate.prompt_name,
        baseline_version=baseline.version,
        candidate_version=candidate.version,
        blocked=blocked,
        green_flag=(not blocked) and improvement_rate > cfg.improvement_green_flag,
        reasons=reasons,
        fail_rate=candidate.fail_rate,
        regression_rate=regression_rate,
        improvement_rate=improvement_rate,
        confidence_drop=conf_drop,
        regressions=regressions,
    )
