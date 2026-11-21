"""Pipeline registration / self-service onboarding from YAML definitions.

Business teams own: extraction fields, validation rules, routing, prompts-examples.
The platform owns: gateway, ingestion, validation framework, quarantine, orchestration base.
A team onboards by submitting a YAML file; `PipelineRegistry.register_*` runs the checks a
platform engineer would otherwise do by hand and returns an OnboardingReport.
"""

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from .contracts import utcnow
from .gateway.quota import AuthRegistry
from .validation import RULES, FieldSpec

# name -> factory(publisher, definition) -> handler(validated_record) -> dict   (registered by domains)
ROUTE_HANDLER_FACTORIES: dict[str, Callable] = {}


def register_route_handler(name: str):
    def deco(factory):
        ROUTE_HANDLER_FACTORIES[name] = factory
        return factory
    return deco


class ExtractionSection(BaseModel):
    task_type: str = "extract"
    fields: dict[str, FieldSpec] = Field(min_length=1)


class RiskSection(BaseModel):
    task_type: str = "classify_risk"
    categories: list[str] = Field(min_length=2)


class ScoringSection(BaseModel):
    task_type: str = "score_fraud"
    logic: str
    complexity: str = "low"


class Layer2Section(BaseModel):
    sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    always_sample_risks: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


class ValidationSection(BaseModel):
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    layer1_rules: list[str] = Field(default_factory=list)
    layer2: Layer2Section = Layer2Section()


class RoutingSection(BaseModel):
    handler: str
    target: str = ""


class WarehouseSection(BaseModel):
    table: str = "extracted_claims"


class PipelineDefinition(BaseModel):
    name: str
    team: str
    domain: str
    schema_version: str = "v1"
    owner: str = ""
    description: str = ""
    expected_daily_documents: int = Field(default=0, ge=0)
    extraction: ExtractionSection
    risk: RiskSection
    scoring: Optional[ScoringSection] = None
    validation: ValidationSection = ValidationSection()
    routing: RoutingSection
    warehouse: WarehouseSection = WarehouseSection()

    @model_validator(mode="after")
    def _risk_refs(self):
        bad = [r for r in self.validation.layer2.always_sample_risks if r not in self.risk.categories]
        if bad:
            raise ValueError(f"always_sample_risks not in risk.categories: {bad}")
        return self

    @property
    def extraction_schema(self) -> dict[str, str]:
        """field -> description for the gateway /extract call."""
        return {k: (f.description or k) + f" ({f.type})" for k, f in self.extraction.fields.items()}


class Check(BaseModel):
    name: str
    passed: bool
    message: str = ""


class OnboardingReport(BaseModel):
    pipeline: str
    ok: bool
    checks: list[Check]
    definition: Optional[PipelineDefinition] = None
    registered_at: datetime = Field(default_factory=utcnow)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


CALLS_PER_DOCUMENT = 4   # extract + classify + score + (sampled) validate, rounded up


class PipelineRegistry:
    def __init__(self, auth: AuthRegistry):
        self.auth = auth
        self._pipelines: dict[str, PipelineDefinition] = {}
        self._registered_at: dict[str, datetime] = {}
        self._first_output_at: dict[str, datetime] = {}

    def get(self, name: str) -> PipelineDefinition:
        return self._pipelines[name]

    def names(self) -> list[str]:
        return sorted(self._pipelines)

    def register_file(self, path: str) -> OnboardingReport:
        return self.register_yaml(Path(path).read_text())

    def register_yaml(self, text: str, client_team: Optional[str] = None) -> OnboardingReport:
        data = None
        try:
            data = yaml.safe_load(text)
            definition = PipelineDefinition(**data)
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            name = data.get("name", "?") if isinstance(data, dict) else "?"
            return OnboardingReport(pipeline=name, ok=False,
                                    checks=[Check(name="schema", passed=False, message=str(exc)[:500])])
        checks = self._check(definition)
        if client_team is not None:
            # The runner calls the gateway with this client's key; a mismatch would bill (and
            # quota) another team for this pipeline's traffic.
            same = client_team == definition.team
            checks.append(Check(name="gateway_client_team", passed=same,
                                message="" if same else f"gateway client belongs to team '{client_team}', "
                                                        f"pipeline belongs to '{definition.team}'"))
        ok = all(c.passed for c in checks)
        if ok:
            self._pipelines[definition.name] = definition
            self._registered_at[definition.name] = utcnow()
        return OnboardingReport(pipeline=definition.name, ok=ok, checks=checks,
                                definition=definition if ok else None)

    def _check(self, d: PipelineDefinition) -> list[Check]:
        checks = [Check(name="schema", passed=True)]
        account = self.auth.team(d.team)
        checks.append(Check(name="team_registered", passed=account is not None,
                            message="" if account else f"team '{d.team}' has no gateway account"))
        existing = self._pipelines.get(d.name)
        collision = existing is not None and existing.team != d.team
        checks.append(Check(name="name_available", passed=not collision,
                            message=f"pipeline '{d.name}' is owned by team '{existing.team}'" if collision else ""))
        required = [k for k, f in d.extraction.fields.items() if f.required]
        checks.append(Check(name="has_required_fields", passed=bool(required),
                            message="" if required else "declare at least one required field"))
        wanted = d.validation.layer1_rules + d.validation.layer2.rules
        unknown = [r for r in d.validation.layer1_rules if r not in RULES]
        checks.append(Check(name="layer1_rules_exist", passed=not unknown,
                            message=f"unknown rules: {unknown}" if unknown else ""))
        checks.append(Check(name="layer2_rules_present",
                            passed=bool(d.validation.layer2.rules) or d.validation.layer2.sample_rate == 0,
                            message="layer2 sampling enabled but no semantic rules given"))
        handler_ok = d.routing.handler in ROUTE_HANDLER_FACTORIES
        checks.append(Check(name="route_handler_exists", passed=handler_ok,
                            message="" if handler_ok else f"unknown handler '{d.routing.handler}'; "
                                                          f"available: {sorted(ROUTE_HANDLER_FACTORIES)}"))
        if account and d.expected_daily_documents:
            need = d.expected_daily_documents * CALLS_PER_DOCUMENT
            checks.append(Check(name="quota_sufficient", passed=need <= account.daily_quota,
                                message=f"needs ~{need} gateway calls/day, quota is {account.daily_quota}"
                                if need > account.daily_quota else ""))
        return checks

    # ---- the article's critical metric: time-to-first-production-output ----------------------
    def record_first_output(self, name: str) -> None:
        self._first_output_at.setdefault(name, utcnow())

    def time_to_first_output_days(self, name: str) -> Optional[float]:
        if name not in self._first_output_at or name not in self._registered_at:
            return None
        return (self._first_output_at[name] - self._registered_at[name]).total_seconds() / 86400
