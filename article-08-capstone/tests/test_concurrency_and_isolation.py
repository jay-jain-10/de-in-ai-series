"""Regression tests for review findings: tenant isolation, concurrency, fail-closed degradation,
retryability after failures, error classification, and cost accounting across fallbacks."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import HAIKU_MODEL, SONNET_MODEL
from src.gateway.circuit_breaker import CircuitBreaker
from src.gateway.client import GatewayClient, GatewayError
from src.gateway.llm import AnthropicLLMClient, LLMTransientError
from src.gateway.quota import QuotaExceeded, QuotaManager, TeamAccount, load_teams

from .conftest import CLAIMS_YAML, KEYS, claim_text, make_doc

CLAIMS_CALLER = {"team": "claims", "pipeline": "claims-processing", "task_type": "t"}
COMP_CALLER = {"team": "compliance", "pipeline": "compliance-contracts", "task_type": "t"}
CLASSIFY = {"text": claim_text(), "categories": ["low", "medium", "high"]}
EXTRACT = {"text": claim_text(), "extraction_schema": {"claimant_name": "name", "claim_amount": "amount"}}


# ---- tenant isolation -----------------------------------------------------------------------

def test_cache_is_not_shared_across_teams(stack):
    a = stack.http.post("/v1/classify", json={**CLASSIFY, "caller": CLAIMS_CALLER},
                        headers={"X-API-Key": KEYS["claims"]}).json()
    b = stack.http.post("/v1/classify", json={**CLASSIFY, "caller": COMP_CALLER},
                        headers={"X-API-Key": KEYS["compliance"]}).json()
    # Same content, different tenant: must be a fresh call, not a hit revealing team A's traffic.
    assert a["cached"] is False and b["cached"] is False
    assert len(stack.llm.calls) == 2
    again = stack.http.post("/v1/classify", json={**CLASSIFY, "caller": COMP_CALLER},
                            headers={"X-API-Key": KEYS["compliance"]}).json()
    assert again["cached"] is True   # still cached within a team


def test_cost_report_does_not_leak_other_teams_totals(stack):
    stack.http.post("/v1/extract", json={**EXTRACT, "caller": CLAIMS_CALLER}, headers={"X-API-Key": KEYS["claims"]})
    r = stack.http.get("/v1/cost-report", headers={"X-API-Key": KEYS["compliance"]}).json()
    assert r["team"] == "compliance" and r["report"] == {} and r["by_pipeline"] == {}


def test_onboarding_rejects_gateway_client_of_another_team(stack):
    wrong = GatewayClient(stack.http, KEYS["compliance"], team="compliance", pipeline="claims-processing")
    report = stack.platform.onboard(CLAIMS_YAML, wrong)
    assert not report.ok and "gateway_client_team" in [c.name for c in report.failures()]
    assert "claims-processing" not in stack.platform.runners


def test_placeholder_keys_from_env_example_are_refused():
    with pytest.raises(ValueError, match="placeholder"):
        load_teams("config/teams.yaml", env={"CLAIMS_TEAM_API_KEY": "change-me-claims"})


# ---- concurrency ------------------------------------------------------------------------------

class _SlowDict(dict):
    """Widens the read-modify-write window so an unlocked quota check races reliably."""

    def get(self, *a, **kw):
        v = super().get(*a, **kw)
        time.sleep(0.002)
        return v


def test_quota_is_not_oversubscribed_under_concurrency():
    qm = QuotaManager(clock=lambda: 1_000_000.0)
    qm._counts = _SlowDict()
    acct = TeamAccount.create("data", "k", daily_quota=5)
    ok, rejected = [], []

    def call(_):
        try:
            qm.consume(acct)
            ok.append(1)
        except QuotaExceeded:
            rejected.append(1)

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(call, range(40)))
    assert len(ok) == 5 and len(rejected) == 35 and qm.used("data") == 5


def test_gateway_serves_requests_concurrently(stack):
    """Blocking LLM calls must not stall the event loop: two requests have to be in flight at
    once. With an `async def` handler doing blocking work this barrier times out.
    `with TestClient(...)` matters: it runs ONE event loop for all requests, like uvicorn; a bare
    TestClient starts a fresh loop per request and would hide the bug."""
    barrier = threading.Barrier(2, timeout=3)
    inner = stack.llm.complete

    def blocking_complete(model, prompt, max_tokens):
        barrier.wait()
        return inner(model, prompt, max_tokens)

    stack.llm.complete = blocking_complete
    h = {"X-API-Key": KEYS["claims"]}
    with TestClient(stack.app) as client, ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(client.post, "/v1/classify",
                          json={**CLASSIFY, "caller": CLAIMS_CALLER, "text": f"doc {i}"}, headers=h)
                for i in range(2)]
        codes = [f.result(timeout=10).status_code for f in futs]
    assert codes == [200, 200]


def test_half_open_breaker_admits_a_single_probe():
    t = {"now": 0.0}
    cb = CircuitBreaker(min_calls=2, cooldown_seconds=30, clock=lambda: t["now"])
    cb.record_failure(); cb.record_failure()
    t["now"] = 31
    assert cb.state == "half_open"
    assert cb.allow() is True
    assert cb.allow() is False and cb.allow() is False     # no stampede while the probe is out
    cb.release_probe()                                     # probe ended without a verdict
    assert cb.allow() is True
    t["now"] = 100                                         # stuck probe expires after cooldown
    assert cb.allow() is True
    cb.record_success()
    assert cb.state == "closed" and cb.allow()


# ---- error classification / swallowing ----------------------------------------------------------

def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize("name,code,transient", [
    ("BadRequestError", 400, False), ("AuthenticationError", 401, False),
    ("RateLimitError", 429, True), ("InternalServerError", 500, True), ("APIStatusError", 529, True),
])
def test_anthropic_adapter_only_maps_retryable_errors_to_transient(name, code, transient):
    import anthropic
    sdk = MagicMock()
    sdk.messages.create.side_effect = _status_error(getattr(anthropic, name), code)
    c = AnthropicLLMClient(client=sdk)
    if transient:
        with pytest.raises(LLMTransientError):
            c.complete(HAIKU_MODEL, "hi", 10)
    else:
        with pytest.raises(anthropic.APIStatusError) as exc:
            c.complete(HAIKU_MODEL, "hi", 10)
        assert not isinstance(exc.value, LLMTransientError)


def test_non_transient_llm_error_is_500_logged_and_does_not_trip_breaker(stack, caplog):
    stack.llm.queue = [ValueError("bad request")] * 30
    h = {"X-API-Key": KEYS["claims"]}
    with caplog.at_level(logging.ERROR, logger="src.gateway.app"):
        for i in range(12):
            r = stack.http.post("/v1/classify", json={**CLASSIFY, "caller": CLAIMS_CALLER, "text": f"x{i}"},
                                headers=h)
            assert r.status_code == 500
    assert stack.breaker.state == "closed"           # one team's bad requests can't open it for all
    assert len(stack.llm.calls) == 12                # not retried
    assert any("ValueError" in (rec.exc_text or "") for rec in caplog.records)
    assert stack.service.metrics.counter("gateway_errors_total", status="5xx") == 12


# ---- cost accounting ------------------------------------------------------------------------------

def test_response_cost_prices_each_attempt_at_its_own_model(stack):
    stack.llm.queue = ["garbage"] * 3                # 3 Sonnet attempts fail, Haiku fallback succeeds
    r = stack.http.post("/v1/extract", json={**EXTRACT, "caller": CLAIMS_CALLER},
                        headers={"X-API-Key": KEYS["claims"]}).json()
    assert r["model"] == HAIKU_MODEL
    events = stack.service.cost.events
    assert [e.model for e in events] == [SONNET_MODEL] * 3 + [HAIKU_MODEL]
    assert r["cost_usd"] == pytest.approx(sum(e.cost_usd for e in events))
    haiku_only = stack.service.cost.price(HAIKU_MODEL, r["input_tokens"], r["output_tokens"])
    assert r["cost_usd"] > haiku_only


# ---- pipeline: retryability + fail-closed degradation -----------------------------------------------

def test_document_is_retryable_after_quota_429(claims_stack):
    s = claims_stack
    s.runner.layer2.sample_rate = 0.0
    good_gw = s.runner.gw
    s.runner.gw = GatewayClient(s.http, KEYS["data"], team="data", pipeline="claims-processing")  # 3/day
    doc = make_doc(claim_text() + "filler words " * 12000)
    with pytest.raises(GatewayError) as exc:
        s.runner.run(doc)
    assert exc.value.status == 429
    s.runner.gw = good_gw                             # orchestrator retries later with quota available
    res = s.runner.run(doc)
    assert res.outcome == "loaded"                    # previously: "duplicate", document lost


def test_degraded_risk_classification_quarantines_instead_of_defaulting_to_low(claims_stack):
    s = claims_stack
    s.runner.layer2.sample_rate = 0.0
    real = s.llm._classify
    s.llm._classify = lambda prompt: {"category": "bogus", "confidence": 0.9}   # every attempt invalid
    res = s.runner.run(make_doc(claim_text(amount="250,000.00")))   # would be "high" risk
    s.llm._classify = real
    assert res.outcome == "quarantined" and "risk classification unavailable" in res.reasons[0]
    assert s.platform.warehouse.rows("extracted_claims") == []
    assert s.publisher.values("claims.mainframe") == []


def test_degraded_fraud_score_is_not_persisted_as_a_real_score(claims_stack):
    s = claims_stack
    s.runner.layer2.sample_rate = 0.0
    s.llm._score = lambda prompt: {"score": 7}        # out of range on every attempt -> degraded
    res = s.runner.run(make_doc())
    assert res.outcome == "loaded"
    assert s.platform.warehouse.get("fraud_scores", document_id=res.document_id) is None
    assert s.publisher.values("claims.mainframe")[0]["fraud_score"] is None
