"""AI Gateway: auth, routing, caching, quotas, cost attribution, retries, circuit breaker."""

import json

import pytest

from src.config import HAIKU_MODEL, SONNET_MODEL
from src.gateway.llm import LLMTransientError

from .conftest import KEYS, claim_text

CALLER = {"team": "claims", "pipeline": "claims-processing", "task_type": "t"}
H = {"X-API-Key": KEYS["claims"]}

EXTRACT = {"caller": CALLER, "text": claim_text(),
           "extraction_schema": {"claimant_name": "name", "claim_amount": "amount"}}
CLASSIFY = {"caller": CALLER, "text": claim_text(), "categories": ["low", "medium", "high"]}
SUMMARIZE = {"caller": CALLER, "text": claim_text(), "max_words": 20}
VALIDATE = {"caller": CALLER, "record": {"claim_amount": 5}, "rules": ["amount plausible"]}
SCORE = {"caller": CALLER, "record": {"claim_amount": 5}, "scoring_logic": "fraud"}


# ---- auth ----------------------------------------------------------------------------------

def test_missing_and_invalid_key_rejected(stack):
    assert stack.http.post("/v1/extract", json=EXTRACT).status_code == 401
    assert stack.http.post("/v1/extract", json=EXTRACT, headers={"X-API-Key": "nope"}).status_code == 401
    assert stack.llm.calls == []


def test_caller_team_must_match_key(stack):
    r = stack.http.post("/v1/extract", json=EXTRACT, headers={"X-API-Key": KEYS["compliance"]})
    assert r.status_code == 403


def test_invalid_request_body_rejected(stack):
    bad = {**CLASSIFY, "categories": ["only-one"]}
    assert stack.http.post("/v1/classify", json=bad, headers=H).status_code == 422


# ---- endpoints + model routing --------------------------------------------------------------

@pytest.mark.parametrize("path,body,model,key", [
    ("extract", EXTRACT, SONNET_MODEL, "fields"),
    ("classify", CLASSIFY, HAIKU_MODEL, "category"),
    ("summarize", SUMMARIZE, HAIKU_MODEL, "summary"),
    ("validate", VALIDATE, SONNET_MODEL, "valid"),
    ("score", SCORE, HAIKU_MODEL, "score"),
    ("score", {**SCORE, "complexity": "high"}, SONNET_MODEL, "score"),
])
def test_endpoint_routes_to_expected_model(stack, path, body, model, key):
    r = stack.http.post(f"/v1/{path}", json=body, headers=H)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == model and stack.llm.calls[-1]["model"] == model
    assert key in data["result"] and data["degraded"] is False and data["cost_usd"] > 0
    assert data["prompt_version"] == "1.0"


def test_extract_result_contains_every_schema_field(stack):
    r = stack.http.post("/v1/extract", json=EXTRACT, headers=H).json()
    assert set(r["result"]["fields"]) == {"claimant_name", "claim_amount"}
    assert r["result"]["fields"]["claimant_name"] == "Jane Doe"


# ---- caching --------------------------------------------------------------------------------

def test_second_identical_request_is_cache_hit_from_l1(stack):
    first = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    second = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    assert first["cached"] is False and second["cached"] is True and second["cache_tier"] == "L1"
    assert second["result"] == first["result"] and len(stack.llm.calls) == 1
    assert second["cost_usd"] == 0.0


def test_cache_key_ignores_caller_metadata(stack):
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)
    other = {**CLASSIFY, "caller": {**CALLER, "task_type": "different"}}
    assert stack.http.post("/v1/classify", json=other, headers=H).json()["cached"] is True


def test_l2_hit_promotes_to_l1_and_ttls_expire(stack):
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)
    assert len(stack.l1) == 1 and len(stack.l2) == 1
    stack.clock["t"] += 2 * 24 * 3600          # L1 (1 day) expired, L2 (30 days) alive
    r = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    assert r["cached"] and r["cache_tier"] == "L2" and len(stack.llm.calls) == 1
    assert stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()["cache_tier"] == "L1"
    stack.clock["t"] += 31 * 24 * 3600         # both expired -> cold path
    assert stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()["cached"] is False
    assert len(stack.llm.calls) == 2


# ---- quotas ---------------------------------------------------------------------------------

def test_quota_exceeded_returns_429_with_clear_error(stack):
    dh = {"X-API-Key": KEYS["data"]}
    body = {**CLASSIFY, "caller": {**CALLER, "team": "data"}}
    for i in range(3):
        b = {**body, "text": f"doc {i}"}
        assert stack.http.post("/v1/classify", json=b, headers=dh).status_code == 200
    r = stack.http.post("/v1/classify", json={**body, "text": "doc 4"}, headers=dh)
    assert r.status_code == 429
    assert r.json()["error"] == "quota_exceeded" and r.json()["daily_limit"] == 3
    assert "Retry-After" in r.headers
    # quota resets next UTC day
    stack.clock["t"] += 86400
    assert stack.http.post("/v1/classify", json={**body, "text": "doc 5"}, headers=dh).status_code == 200


def test_usage_endpoint(stack):
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)
    u = stack.http.get("/v1/usage", headers=H).json()
    assert u["team"] == "claims" and u["calls_today"] == 1 and u["daily_quota"] == 5000 and u["spend_usd"] > 0


# ---- cost attribution -----------------------------------------------------------------------

def test_cost_attributed_per_team_pipeline_task(stack):
    stack.http.post("/v1/extract", json=EXTRACT, headers=H)
    other = {**CLASSIFY, "caller": {"team": "compliance", "pipeline": "compliance-contracts",
                                    "task_type": "classify_doc"}}
    stack.http.post("/v1/classify", json=other, headers={"X-API-Key": KEYS["compliance"]})
    rep = stack.service.cost.report()
    assert set(rep["by_team"]) == {"claims", "compliance"}
    assert abs(sum(t["share"] for t in rep["by_team"].values()) - 1.0) < 0.01
    assert "claims/claims-processing" in rep["by_pipeline"]
    assert "compliance/compliance-contracts/classify_doc" in rep["by_task_type"]
    assert rep["by_team"]["claims"]["usd"] > rep["by_team"]["compliance"]["usd"]  # Sonnet > Haiku


def test_cost_events_streamed_to_kafka_topic(stack):
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)   # cache hit also logged, cost 0
    events = stack.publisher.values("gateway.cost_events")
    assert len(events) == 2
    assert events[0]["team"] == "claims" and events[0]["cost_usd"] > 0 and events[0]["cached"] is False
    assert events[1]["cached"] is True and events[1]["cost_usd"] == 0


def test_cost_math_matches_pricing(stack):
    stack.service.cost.pricing[HAIKU_MODEL] = {"input": 1.0, "output": 5.0}
    assert stack.service.cost.price(HAIKU_MODEL, 1_000_000, 1_000_000) == pytest.approx(6.0)


def test_budget_alerts(stack):
    stack.http.post("/v1/extract", json=EXTRACT, headers=H)
    spent = stack.service.cost.total("claims")
    alerts = stack.service.cost.budget_alerts({"claims": spent * 1.1, "compliance": 100.0})
    assert alerts[0]["team"] == "claims" and alerts[0]["severity"] == "warning"
    assert stack.service.cost.budget_alerts({"claims": spent * 0.5})[0]["severity"] == "exceeded"


def test_cost_report_endpoint_is_scoped_to_caller_team(stack):
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)
    other = {**CLASSIFY, "caller": {**CALLER, "team": "compliance"}, "text": "x"}
    stack.http.post("/v1/classify", json=other, headers={"X-API-Key": KEYS["compliance"]})
    r = stack.http.get("/v1/cost-report", headers=H).json()
    assert r["team"] == "claims" and all(k.startswith("claims/") for k in r["by_pipeline"])


# ---- retries / structured output ------------------------------------------------------------

def test_transient_errors_are_retried_with_backoff(stack):
    stack.llm.queue = [LLMTransientError("429"), LLMTransientError("503")]
    r = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    assert r["degraded"] is False and r["attempts"] == 3
    assert stack.sleeps == [1, 2]


def test_invalid_json_retried_with_explicit_instruction(stack):
    stack.llm.queue = ["Sure! here is the answer"]
    r = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    assert r["degraded"] is False and r["attempts"] == 2
    assert "ONLY the JSON object" in stack.llm.calls[1]["prompt"]
    assert "ONLY the JSON object" not in stack.llm.calls[0]["prompt"]


def test_schema_violation_counts_as_invalid_output(stack):
    stack.llm.queue = [{"category": "not-a-category", "confidence": 0.9}]
    r = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    assert r["result"]["category"] in ("low", "medium", "high") and r["attempts"] == 2


def test_extract_falls_back_to_haiku_after_sonnet_keeps_failing(stack):
    stack.llm.queue = ["garbage", "garbage", "garbage"]   # 3 primary attempts (Sonnet) fail
    r = stack.http.post("/v1/extract", json=EXTRACT, headers=H).json()
    assert [c["model"] for c in stack.llm.calls] == [SONNET_MODEL] * 3 + [HAIKU_MODEL]
    assert r["model"] == HAIKU_MODEL and r["degraded"] is False and r["attempts"] == 4


def test_all_attempts_fail_returns_degraded_default_and_is_not_cached(stack):
    stack.llm.queue = ["bad"] * 4
    r = stack.http.post("/v1/extract", json=EXTRACT, headers=H).json()
    assert r["degraded"] is True and r["result"]["confidence"] == 0.0
    assert r["result"]["fields"] == {"claimant_name": None, "claim_amount": None}
    again = stack.http.post("/v1/extract", json=EXTRACT, headers=H).json()
    assert again["degraded"] is False and again["cached"] is False   # degraded result never cached


def test_fenced_json_is_accepted(stack):
    stack.llm.queue = ['```json\n{"summary": "ok"}\n```']
    r = stack.http.post("/v1/summarize", json=SUMMARIZE, headers=H).json()
    assert r["result"]["summary"] == "ok" and r["attempts"] == 1


# ---- circuit breaker --------------------------------------------------------------------------

def _outage(stack, n=60):
    stack.llm.queue = [LLMTransientError("down")] * n


def test_breaker_opens_on_error_rate_and_serves_defaults_without_calling_api(stack):
    _outage(stack)
    for i in range(4):   # Haiku: 3 attempts per request -> 12 failures >= min_calls
        stack.http.post("/v1/classify", json={**CLASSIFY, "text": f"d{i}"}, headers=H)
    assert stack.breaker.state == "open"
    calls_before = len(stack.llm.calls)
    r = stack.http.post("/v1/classify", json={**CLASSIFY, "text": "new"}, headers=H).json()
    assert r["degraded"] is True and r["result"]["confidence"] == 0.0
    assert len(stack.llm.calls) == calls_before
    assert stack.http.get("/health").json()["circuit_breaker"] == "open"


def test_open_breaker_still_serves_cached_responses(stack):
    ok = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    _outage(stack)
    for i in range(4):   # Haiku: 3 attempts per request -> 12 failures >= min_calls
        stack.http.post("/v1/classify", json={**CLASSIFY, "text": f"d{i}"}, headers=H)
    assert stack.breaker.state == "open"
    r = stack.http.post("/v1/classify", json=CLASSIFY, headers=H).json()
    assert r["cached"] is True and r["degraded"] is False and r["result"] == ok["result"]


def test_breaker_recovers_after_cooldown(stack):
    _outage(stack)
    for i in range(4):   # Haiku: 3 attempts per request -> 12 failures >= min_calls
        stack.http.post("/v1/classify", json={**CLASSIFY, "text": f"d{i}"}, headers=H)
    assert stack.breaker.state == "open"
    stack.llm.queue.clear()
    stack.clock["t"] += 31
    r = stack.http.post("/v1/classify", json={**CLASSIFY, "text": "after"}, headers=H).json()
    assert r["degraded"] is False and stack.breaker.state == "closed"


def test_health_and_metrics(stack):
    stack.http.post("/v1/classify", json=CLASSIFY, headers=H)
    assert stack.http.get("/health").json()["status"] == "ok"
    counters = stack.http.get("/metrics").json()["counters"]
    assert any(k.startswith("gateway_requests_total") for k in counters)
