"""Insurance-claims domain logic (owned by the claims team, not the platform team).

Registers: domain validation rules, the mainframe route handler, and the ClaimRecord contract.
"""

import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from ..contracts import ValidatedRecord
from ..registry import register_route_handler
from ..validation import register_rule

INVALID_PROCEDURE_CODES = {"99999"}   # article: procedure code 99999 is invalid


class ClaimRecord(BaseModel):
    """Domain contract at the claims -> mainframe boundary."""
    document_id: str
    claimant_name: str
    policy_number: str = Field(pattern=r"^POL-\d{6}$")
    incident_date: date
    claim_amount: float = Field(gt=0)
    procedure_code: Optional[str] = None
    diagnosis_code: Optional[str] = None
    medical_records_referenced: list[str] = Field(default_factory=list)
    witnesses: list[str] = Field(default_factory=list)
    risk_class: str
    fraud_score: Optional[float] = None


@register_rule("claims.amount_logic")
def validate_claims_amount_logic(fields: dict, ctx: dict) -> list:
    msgs = []
    amount = fields.get("claim_amount")
    if amount is not None and amount <= 0:
        msgs.append("claim_amount must be greater than zero")
    policy = (ctx.get("policy_lookup") or (lambda _: None))(fields.get("policy_number"))
    if policy and amount is not None and amount > policy.get("coverage_limit", float("inf")):
        msgs.append(f"claim_amount {amount} exceeds policy coverage limit {policy['coverage_limit']}")
    return msgs


@register_rule("claims.procedure_code_valid")
def validate_procedure_code(fields: dict, ctx: dict) -> list:
    code = fields.get("procedure_code")
    if code is None:
        return []
    if code in INVALID_PROCEDURE_CODES or not re.fullmatch(r"\d{5}", code):
        return [f"procedure_code {code!r} is not a valid procedure code"]
    return []


@register_rule("claims.incident_not_future")
def validate_incident_date(fields: dict, ctx: dict) -> list:
    d = fields.get("incident_date")
    today = ctx.get("today") or date.today()
    if d and date.fromisoformat(d) > today:
        return [f"incident_date {d} is in the future"]
    return []


@register_rule("claims.policy_exists")
def validate_policy_fk(fields: dict, ctx: dict) -> list:
    """Foreign-key integrity against the policy system."""
    lookup = ctx.get("policy_lookup")
    if lookup is None or fields.get("policy_number") is None:
        return []
    return [] if lookup(fields["policy_number"]) else [f"policy {fields['policy_number']} not found"]


@register_route_handler("mainframe")
def mainframe_handler_factory(publisher, definition):
    """Route clean claims to the mainframe via a Kafka topic (a mainframe connector consumes it)."""
    topic = definition.routing.target or "claims.mainframe"

    def route(v: ValidatedRecord) -> dict:
        f = v.record.fields
        claim = ClaimRecord(document_id=v.record.document_id, risk_class=v.risk_class,
                            fraud_score=v.fraud_score,
                            **{k: f.get(k) for k in ("claimant_name", "policy_number", "incident_date",
                                                     "claim_amount", "procedure_code", "diagnosis_code")},
                            medical_records_referenced=f.get("medical_records_referenced") or [],
                            witnesses=f.get("witnesses") or [])
        publisher.publish(topic, claim.document_id, claim.model_dump(mode="json"))
        return {"routed_to": topic, "key": claim.document_id}
    return route
