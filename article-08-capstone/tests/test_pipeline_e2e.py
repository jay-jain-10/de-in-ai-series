"""End to end: ingest -> process -> gateway -> L1 -> classify -> L2 -> warehouse/mainframe,
with quarantine, human review, lineage, backfill and a second team onboarding."""

import pytest

from src.config import HAIKU_MODEL, SONNET_MODEL
from src.contracts import ReviewDecision, ReviewDecisionType, SourceType
from src.document_processor import ImageExtractor
from src.domains.claims import ClaimRecord
from src.gateway.client import GatewayClient, GatewayError
from src.observability import PlatformDashboards, SLAMonitor

from .conftest import COMPLIANCE_YAML, KEYS, claim_text, make_doc


def tasks(stack):
    return [c["task"] for c in stack.llm.calls]


def no_l2(stack):
    stack.runner.layer2.sample_rate = 0.0


def force_l2(stack):
    stack.runner.layer2.sample_rate = 1.0


# ---- happy path --------------------------------------------------------------------------------

def test_clean_claim_flows_to_warehouse_and_mainframe(claims_stack):
    s = claims_stack
    no_l2(s)
    res = s.runner.run(make_doc())
    assert res.outcome == "loaded" and res.risk_class == "low" and not res.layer2_sampled
    row = s.platform.warehouse.get("extracted_claims", document_id=res.document_id)
    assert row["fields"]["claim_amount"] == 4500.0 and row["fields"]["incident_date"] == "2026-10-01"
    assert row["schema_version"] == "v1" and row["layer2_status"] == "not_sampled"
    assert row["processing_version"] == s.platform.config.processing_version
    raw = s.platform.warehouse.get("raw_documents", document_id=res.document_id)
    assert "Jane Doe" in raw["text"] and raw["source_type"] == "email"
    assert s.platform.warehouse.get("fraud_scores", document_id=res.document_id)["score"] == 0.1
    # Kafka: ingest envelope in and clean claim out, validated by the domain contract
    assert len(s.publisher.values("documents.incoming")) == 1
    out = s.publisher.values("claims.mainframe")
    assert len(out) == 1 and ClaimRecord(**out[0]).claim_amount == 4500.0
    # model routing as in the article: extract -> Sonnet, classify -> Haiku
    by_task = {c["task"]: c["model"] for c in s.llm.calls}
    assert by_task == {"extract": SONNET_MODEL, "classify": HAIKU_MODEL, "score": HAIKU_MODEL}


def test_cost_is_attributed_to_claims_team_and_pipeline(claims_stack):
    s = claims_stack
    no_l2(s)
    s.runner.run(make_doc())
    rep = s.service.cost.report()
    assert set(rep["by_team"]) == {"claims"}
    assert {"claims/claims-processing/extract_claim_facts", "claims/claims-processing/classify_risk",
            "claims/claims-processing/score_fraud"} <= set(rep["by_task_type"])


def test_end_to_end_lineage(claims_stack):
    s = claims_stack
    no_l2(s)
    res = s.runner.run(make_doc())
    trace = s.platform.lineage.trace(res.document_id)
    assert [t.step for t in trace] == ["ingest", "process", "extract", "validate_layer1", "classify_risk",
                                      "score_fraud", "load_warehouse", "route"]
    ext = trace[2]
    assert ext.model_used == SONNET_MODEL and ext.token_count > 0 and ext.cost_usd > 0
    assert ext.confidence_score == 0.95 and ext.processing_version == s.platform.config.processing_version
    assert ext.details["prompt_version"] == "1.0"
    totals = s.platform.lineage.totals(res.document_id)
    assert totals["cost_usd"] == pytest.approx(s.service.cost.total("claims"))
    assert SONNET_MODEL in totals["models"] and HAIKU_MODEL in totals["models"]
    assert len(s.platform.lineage.explain(res.document_id)) == len(trace)
    persisted = [r for r in s.platform.warehouse.rows("lineage") if r["document_id"] == res.document_id]
    assert len(persisted) == len(trace)


def test_high_value_claim_is_always_semantically_validated(claims_stack):
    s = claims_stack
    no_l2(s)    # base sample rate 0, but risk=high is always sampled
    res = s.runner.run(make_doc(claim_text(amount="250,000.00")))
    assert res.outcome == "loaded" and res.risk_class == "high" and res.layer2_sampled
    validate_call = [c for c in s.llm.calls if c["task"] == "validate"][0]
    assert validate_call["model"] == SONNET_MODEL
    assert s.platform.warehouse.get("extracted_claims", document_id=res.document_id)["layer2_status"] == "passed"


# ---- quarantine paths ------------------------------------------------------------------------------

def test_layer1_failure_quarantines_fast_without_spending_on_later_stages(claims_stack):
    s = claims_stack
    res = s.runner.run(make_doc(claim_text(procedure="99999")))
    assert res.outcome == "quarantined" and res.stage == "layer1"
    assert any("procedure_code" in r for r in res.reasons)
    assert tasks(s) == ["extract"]                      # no classify/score/validate spend
    assert s.platform.warehouse.rows("extracted_claims") == []
    assert s.publisher.values("claims.mainframe") == []
    assert len(s.platform.quarantine.pending()) == 1
    assert s.platform.metrics.counter("pipeline_quarantine_total", stage="layer1") == 1


def test_low_extraction_confidence_is_quarantined(claims_stack):
    s = claims_stack
    s.llm.extract_confidence = 0.5
    res = s.runner.run(make_doc())
    assert res.outcome == "quarantined" and any("extraction_confidence" in r for r in res.reasons)


def test_layer2_semantic_failure_quarantines_with_reason(claims_stack):
    s = claims_stack
    force_l2(s)
    s.llm.validate_result = {"valid": False, "issues": ["procedure 99213 does not match diagnosis"],
                             "confidence": 0.8}
    res = s.runner.run(make_doc())
    assert res.outcome == "quarantined" and res.stage == "layer2"
    assert "does not match diagnosis" in res.reasons[0] and res.risk_class == "low"
    assert s.platform.warehouse.rows("extracted_claims") == []
    assert s.platform.warehouse.get("validation_results", document_id=res.document_id, layer=2)["passed"] is False


def test_layer2_unavailable_is_quarantined_not_silently_passed(claims_stack):
    s = claims_stack
    force_l2(s)
    s.llm.validate_result = "not-a-json-object"          # gateway exhausts retries -> degraded default
    res = s.runner.run(make_doc())
    assert res.outcome == "quarantined" and res.stage == "layer2"
    assert "unavailable" in res.reasons[0]


def test_partial_ocr_extraction_is_quarantined_at_confidence_0_6(claims_stack):
    s = claims_stack
    s.platform.processor.register("image/", ImageExtractor(lambda b: [claim_text(), None]))
    res = s.runner.run(make_doc(data=b"img", mime="image/png", source=SourceType.FAX))
    assert res.outcome == "quarantined"
    rec = s.platform.quarantine.get(res.quarantine_id)
    assert rec.extraction_confidence == 0.6 and "extraction_confidence 0.60" in rec.reasons[0]


def test_unreadable_document_quarantined_at_extraction_stage(claims_stack):
    s = claims_stack
    s.platform.processor.register("image/", ImageExtractor(lambda b: [None]))
    res = s.runner.run(make_doc(data=b"img", mime="image/png"))
    assert res.outcome == "quarantined" and res.stage == "extraction" and s.llm.calls == []


def test_gateway_outage_degrades_to_quarantine_not_bad_data(claims_stack):
    from src.gateway.llm import LLMTransientError
    s = claims_stack
    s.llm.queue = [LLMTransientError("down")] * 40
    res = s.runner.run(make_doc(claim_text(name="Outage Case")))
    assert res.outcome == "quarantined" and s.platform.warehouse.rows("extracted_claims") == []
    assert s.platform.metrics.counter("gateway_degraded_total") >= 1


def test_gateway_quota_exhaustion_is_raised_for_retry_not_quarantined(claims_stack):
    s = claims_stack
    s.runner.gw = GatewayClient(s.http, KEYS["data"], team="data", pipeline="claims-processing")  # quota 3/day
    big = claim_text() + "filler words " * 12000       # many chunks -> more than 3 gateway calls
    with pytest.raises(GatewayError) as exc:
        s.runner.run(make_doc(big))
    assert exc.value.status == 429 and s.platform.quarantine.pending() == []


# ---- idempotency / chunking ------------------------------------------------------------------------

def test_duplicate_document_is_not_reprocessed(claims_stack):
    s = claims_stack
    no_l2(s)
    first = s.runner.run(make_doc(source_id="email-1"))
    n = len(s.llm.calls)
    again = s.runner.run(make_doc(source_id="email-2"))      # same content, different source
    assert first.outcome == "loaded" and again.outcome == "duplicate" and len(s.llm.calls) == n
    assert len(s.platform.warehouse.rows("extracted_claims")) == 1
    assert len(s.publisher.values("claims.mainframe")) == 1


def test_rerunning_a_stage_is_safe_merge_not_duplicate(claims_stack):
    s = claims_stack
    no_l2(s)
    res = s.runner.run(make_doc())
    s.runner.reprocess([res.document_id])
    s.runner.reprocess([res.document_id])
    assert len(s.platform.warehouse.rows("extracted_claims")) == 1
    assert len(s.platform.warehouse.rows("raw_documents")) == 1


def test_long_document_is_chunked_and_extractions_merged(claims_stack):
    s = claims_stack
    no_l2(s)
    long_doc = claim_text() + "narrative sentence about the incident. " * 2000     # ~19k tokens
    res = s.runner.run(make_doc(long_doc))
    extract_steps = [t for t in s.platform.lineage.trace(res.document_id) if t.step == "extract"]
    n_chunks = len(extract_steps)
    assert n_chunks >= 5 and res.outcome == "loaded"
    assert extract_steps[0].details["chunk"] == f"1/{n_chunks}"
    # repeated narrative chunks are identical -> served by the gateway cache, not the API
    assert sum(c["task"] == "extract" for c in s.llm.calls) < n_chunks
    row = s.platform.warehouse.get("extracted_claims", document_id=res.document_id)
    assert row["fields"]["claimant_name"] == "Jane Doe"        # taken from first chunk, later nulls ignored


# ---- human review loop ---------------------------------------------------------------------------------

def quarantine_unknown_policy(s):
    no_l2(s)
    res = s.runner.run(make_doc(claim_text(policy="POL-999999")))
    assert res.outcome == "quarantined" and "not found" in " ".join(res.reasons)
    return res


def decide(s, res, kind, **kw):
    return s.platform.quarantine.resolve(ReviewDecision(quarantine_id=res.quarantine_id, reviewer="alice",
                                                        decision=kind, **kw))


def test_reviewer_correction_releases_record_to_warehouse_and_mainframe(claims_stack):
    s = claims_stack
    res = quarantine_unknown_policy(s)
    view = s.platform.quarantine.review_view(res.quarantine_id)
    assert view["extracted"]["policy_number"] == "POL-999999" and "Jane Doe" in view["document"]
    decide(s, res, ReviewDecisionType.CORRECT, corrected_fields={"policy_number": "POL-123456"})
    row = s.platform.warehouse.get("extracted_claims", document_id=res.document_id)
    assert row["fields"]["policy_number"] == "POL-123456" and row["layer2_status"] == "human_reviewed"
    assert len(s.publisher.values("claims.mainframe")) == 1
    steps = [t.step for t in s.platform.lineage.trace(res.document_id)]
    assert "quarantine" in steps and "human_review" in steps and steps[-1] == "route"
    assert s.platform.quarantine.pending() == []


def test_reviewer_confirm_of_low_confidence_extraction_loads_it(claims_stack):
    s = claims_stack
    s.platform.processor.register("image/", ImageExtractor(lambda b: [claim_text(), None]))
    res = s.runner.run(make_doc(data=b"img", mime="image/png"))
    assert res.outcome == "quarantined"
    decide(s, res, ReviewDecisionType.CONFIRM)
    assert s.platform.warehouse.get("extracted_claims", document_id=res.document_id) is not None
    assert s.platform.quarantine.extraction_accuracy() == 1.0


def test_upstream_decision_does_not_load(claims_stack):
    s = claims_stack
    res = quarantine_unknown_policy(s)
    decide(s, res, ReviewDecisionType.UPSTREAM, note="agent sent wrong policy")
    assert s.platform.warehouse.rows("extracted_claims") == []
    assert s.platform.quarantine.extraction_accuracy() is None


def test_bad_correction_is_requarantined_rather_than_loaded(claims_stack):
    s = claims_stack
    res = quarantine_unknown_policy(s)
    decide(s, res, ReviewDecisionType.CORRECT, corrected_fields={"claim_amount": "-10"})
    assert s.platform.warehouse.rows("extracted_claims") == []
    pending = s.platform.quarantine.pending()
    assert len(pending) == 1 and "still fails layer 1" in pending[0].reasons[0]


# ---- second team, backfill, observability ------------------------------------------------------------------

def test_second_pipeline_onboards_and_runs_on_same_platform(stack):
    cgw = GatewayClient(stack.http, KEYS["compliance"], team="compliance", pipeline="compliance-contracts")
    assert stack.platform.onboard(COMPLIANCE_YAML, cgw).ok
    runner = stack.platform.runners["compliance-contracts"]
    runner.layer2.sample_rate = 0.0
    doc = make_doc("Vendor: Acme Corp\nEffective: 2026-01-01\nTerm months: 24\nAuto renews: true\n",
                   team="compliance", pipeline="compliance-contracts")
    res = runner.run(doc)
    assert res.outcome == "loaded"
    row = stack.platform.warehouse.get("extracted_claims", document_id=res.document_id)
    assert row["fields"]["term_months"] == 24 and row["fields"]["auto_renews"] is True
    assert len(stack.publisher.values("compliance.workflow")) == 1
    assert set(stack.service.cost.report()["by_team"]) == {"compliance"}


def test_backfill_reprocesses_documents_from_older_processing_version(claims_stack):
    s = claims_stack
    no_l2(s)
    res = s.runner.run(make_doc())
    assert s.runner.stale_documents() == []
    s.platform.set_processing_version("2026.12.0")                 # new prompt/model release
    assert s.runner.stale_documents() == [res.document_id]
    s.l1._data.clear(); s.l2._data.clear()                          # new prompt version => new cache keys
    s.llm.queue = [{"fields": {"claimant_name": "Jane Doe", "policy_number": "POL-123456",
                               "incident_date": "2026-10-01", "claim_amount": "4800",
                               "procedure_code": "99213", "diagnosis_code": "J20.9",
                               "medical_records_referenced": [], "witnesses": []}, "confidence": 0.97}]
    out = s.runner.reprocess(s.runner.stale_documents())
    assert out == [{"document_id": res.document_id, "outcome": "loaded", "changed_fields": ["claim_amount"]}]
    row = s.platform.warehouse.get("extracted_claims", document_id=res.document_id)
    assert row["processing_version"] == "2026.12.0" and row["fields"]["claim_amount"] == 4800.0
    assert s.runner.stale_documents() == [] and len(s.platform.warehouse.rows("extracted_claims")) == 1


def test_backfill_reports_missing_raw_documents(claims_stack):
    assert claims_stack.runner.reprocess(["ghost"]) == [{"document_id": "ghost", "outcome": "missing_raw"}]


def test_cost_events_persist_to_warehouse(claims_stack):
    s = claims_stack
    no_l2(s)
    s.runner.run(make_doc())
    assert s.platform.flush_cost_events(s.service.cost) == 3
    s.platform.flush_cost_events(s.service.cost)                   # idempotent MERGE
    rows = s.platform.warehouse.rows("cost_events")
    assert len(rows) == 3 and {r["team"] for r in rows} == {"claims"}


def test_dashboards_and_slas_after_a_mixed_batch(claims_stack):
    s = claims_stack
    no_l2(s)
    s.runner.run(make_doc(claim_text(name="A")))
    s.runner.run(make_doc(claim_text(name="B")))
    s.runner.run(make_doc(claim_text(name="C", procedure="99999")))
    dash = PlatformDashboards(s.platform.metrics, s.service.cost, s.platform.quarantine)
    q = dash.quality_dashboard()
    assert q["documents_processed"] == 3 and q["quarantine_backlog"] == 1
    assert q["quarantine_rate"] == pytest.approx(1 / 3, abs=1e-3)
    cost = dash.cost_dashboard(budgets={"claims": 15000})
    assert cost["report"]["by_team"]["claims"]["calls"] > 0 and cost["budget_alerts"] == []
    sla = SLAMonitor(s.platform.config.sla, s.platform.metrics, s.service.cost, s.platform.quarantine).evaluate()
    assert sla["all_ok"] and s.platform.registry.time_to_first_output_days("claims-processing") is not None


def test_rejected_documents_never_reach_the_pipeline(claims_stack):
    res = claims_stack.platform.ingestion.ingest_raw({"source_id": "x", "team": "claims",
                                                      "pipeline": "claims-processing", "content_b64": "aGk="})
    assert not res.accepted and claims_stack.llm.calls == []
