import copy
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import ValidationError

from src.config import HAIKU_MODEL, SONNET_MODEL, PlatformConfig
from src.contracts import CallerMetadata, IncomingDocument
from src.gateway.llm import AnthropicLLMClient, LLMTransientError
from src.gateway.quota import AuthRegistry, TeamAccount, load_teams
from src.observability import MetricsRegistry, PlatformDashboards, SLAMonitor
from src.registry import PipelineRegistry
from src.warehouse import DDL, TABLE_KEYS, InMemoryWarehouse, SnowflakeWarehouse

import src.pipeline  # noqa: F401 (registers handlers)

from .conftest import CLAIMS_YAML, COMPLIANCE_YAML, KEYS


def registry(quota=5000, teams=("claims", "compliance")):
    return PipelineRegistry(AuthRegistry([TeamAccount.create(t, "k" + t, daily_quota=quota) for t in teams]))


def mutated(**kw):
    d = yaml.safe_load(CLAIMS_YAML)
    for k, v in kw.items():
        cur = d
        parts = k.split("__")
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = v
    return yaml.safe_dump(d)


# ---- onboarding -----------------------------------------------------------------------------

def test_claims_pipeline_onboards_cleanly():
    r = registry()
    rep = r.register_yaml(CLAIMS_YAML)
    assert rep.ok and all(c.passed for c in rep.checks) and r.names() == ["claims-processing"]
    d = r.get("claims-processing")
    assert d.extraction.fields["claim_amount"].max == 1000000
    assert d.validation.layer2.sample_rate == 0.1 and d.validation.min_confidence == 0.7
    assert "claim_amount" in d.extraction_schema


def test_second_team_onboards_with_yaml_only():
    r = registry()
    assert r.register_yaml(CLAIMS_YAML).ok and r.register_yaml(COMPLIANCE_YAML).ok
    assert r.names() == ["claims-processing", "compliance-contracts"]


def test_register_file(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(CLAIMS_YAML)
    assert registry().register_file(str(p)).ok


@pytest.mark.parametrize("kw,failing", [
    ({"team": "ghost"}, "team_registered"),
    ({"validation__layer1_rules": ["claims.nope"]}, "layer1_rules_exist"),
    ({"routing__handler": "fax_machine"}, "route_handler_exists"),
    ({"validation__layer2__rules": []}, "layer2_rules_present"),
    ({"expected_daily_documents": 5000}, "quota_sufficient"),
])
def test_onboarding_checks_fail_with_actionable_messages(kw, failing):
    rep = registry().register_yaml(mutated(**kw))
    assert not rep.ok and failing in {c.name for c in rep.failures()}
    assert all(c.message for c in rep.failures())


def test_invalid_yaml_schema_and_unknown_risk_reference():
    r = registry()
    assert not r.register_yaml("name: x\nteam: claims").ok
    assert not r.register_yaml(": : :").ok
    rep = r.register_yaml(mutated(validation__layer2__always_sample_risks=["catastrophic"]))
    assert not rep.ok and r.names() == []


def test_failed_registration_does_not_register():
    r = registry()
    r.register_yaml(mutated(team="ghost"))
    assert r.names() == []


def test_pipeline_name_cannot_be_hijacked_by_other_team():
    r = registry()
    r.register_yaml(CLAIMS_YAML)
    rep = r.register_yaml(mutated(team="compliance"))
    assert "name_available" in {c.name for c in rep.failures()}


def test_time_to_first_output_metric():
    r = registry()
    r.register_yaml(CLAIMS_YAML)
    assert r.time_to_first_output_days("claims-processing") is None
    r.record_first_output("claims-processing")
    assert 0 <= r.time_to_first_output_days("claims-processing") < 1


# ---- config / teams / contracts --------------------------------------------------------------

def test_model_ids_follow_article_1_conventions():
    cfg = PlatformConfig()
    assert HAIKU_MODEL == "claude-haiku-4-5-20251001" == cfg.anthropic.classification_model
    assert cfg.anthropic.extraction_model == SONNET_MODEL
    assert cfg.gateway.default_daily_quota == 5000 and cfg.gateway.breaker_error_threshold == 0.10
    assert cfg.gateway.l1_ttl_seconds == 86400 and cfg.gateway.l2_ttl_seconds == 30 * 86400


def test_load_teams_reads_keys_from_env_only(tmp_path):
    accts = load_teams("config/teams.yaml", env={"CLAIMS_TEAM_API_KEY": "secret"})
    assert [a.team for a in accts] == ["claims"] and accts[0].daily_quota == 5000
    assert "secret" not in repr(accts)               # only a hash is stored
    assert AuthRegistry(accts).authenticate("secret").team == "claims"
    assert AuthRegistry(accts).authenticate("other") is None


def test_incoming_document_helpers():
    from datetime import datetime, timezone
    d = IncomingDocument.from_text("abc", source_type="api", source_id="1", timestamp=datetime.now(timezone.utc),
                                   team="t", pipeline="p")
    assert d.content == b"abc" and d.document_size == 3 and len(d.content_hash) == 64
    with pytest.raises(ValidationError):
        IncomingDocument.from_text("abc", source_type="telepathy", source_id="1",
                                   timestamp=datetime.now(timezone.utc), team="t", pipeline="p")


# ---- Anthropic adapter (SDK mocked) ------------------------------------------------------------

def test_anthropic_adapter_maps_response_and_errors():
    import anthropic
    import httpx
    sdk = MagicMock()
    sdk.messages.create.return_value = MagicMock(content=[MagicMock(text='{"a":1}')],
                                                 usage=MagicMock(input_tokens=11, output_tokens=7))
    c = AnthropicLLMClient(client=sdk)
    res = c.complete(HAIKU_MODEL, "hi", 100)
    assert (res.text, res.input_tokens, res.output_tokens) == ('{"a":1}', 11, 7)
    assert sdk.messages.create.call_args.kwargs["model"] == HAIKU_MODEL
    sdk.messages.create.side_effect = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(LLMTransientError):
        c.complete(HAIKU_MODEL, "hi", 100)


# ---- warehouse -------------------------------------------------------------------------------

def test_inmemory_merge_is_idempotent_upsert():
    w = InMemoryWarehouse()
    w.merge("fraud_scores", {"document_id": "d", "score": 0.1, "rationale": "", "model": "m"})
    w.merge("fraud_scores", {"document_id": "d", "score": 0.9, "rationale": "", "model": "m"})
    assert len(w.rows("fraud_scores")) == 1 and w.get("fraud_scores", document_id="d")["score"] == 0.9


def test_snowflake_loader_builds_parameterized_merge():
    conn = MagicMock()
    cur = conn.cursor.return_value
    SnowflakeWarehouse(conn).merge("extracted_claims", {
        "document_id": "d", "pipeline": "p", "fields": {"a": 1}, "risk_class": "low"})
    sql, params = cur.execute.call_args.args
    assert sql.startswith("MERGE INTO extracted_claims") and "ON t.document_id = s.document_id AND t.pipeline = s.pipeline" in sql
    assert params["fields"] == '{"a": 1}' and "'d'" not in sql      # values are bound, not interpolated
    cur.close.assert_called_once()


def test_snowflake_loader_rejects_bad_identifiers():
    w = SnowflakeWarehouse(MagicMock())
    with pytest.raises(ValueError):
        w.merge("users; DROP TABLE x", {"a": 1})
    with pytest.raises(ValueError):
        w.merge("fraud_scores", {"a b": 1})


def test_snowflake_get_maps_columns():
    conn = MagicMock()
    cur = conn.cursor.return_value
    cur.fetchone.return_value = ("d", 0.5)
    cur.description = [("DOCUMENT_ID",), ("SCORE",)]
    assert SnowflakeWarehouse(conn).get("fraud_scores", document_id="d") == {"document_id": "d", "score": 0.5}


def test_ddl_covers_article_tables():
    for t in ("raw_documents", "extracted_claims", "validation_results", "fraud_scores", "cost_events"):
        assert t in TABLE_KEYS and f"TABLE IF NOT EXISTS {t}" in DDL


# ---- observability ----------------------------------------------------------------------------

def test_metrics_registry_label_matching_and_percentiles():
    m = MetricsRegistry()
    m.inc("c", team="a"); m.inc("c", team="b", x="1")
    assert m.counter("c") == 2 and m.counter("c", team="a") == 1
    for v in range(1, 101):
        m.observe("lat", v)
    assert m.percentile(m.observations("lat"), 0.95) == 95
    m.set_gauge("g", 3.0)
    assert m.snapshot()["gauges"]["g"] == 3.0 and m.percentile([], 0.5) is None


def test_sla_monitor_flags_breaches():
    from src.config import SLAConfig
    m = MetricsRegistry()
    sla = SLAMonitor(SLAConfig(), m)
    assert sla.evaluate()["all_ok"]
    for _ in range(100):
        m.observe("pipeline_ingest_to_warehouse_seconds", 5 * 3600)
    r = sla.evaluate()
    assert not r["ingest_to_warehouse"]["ok"] and not r["all_ok"]
    m2 = MetricsRegistry()
    m2.inc("gateway_requests_total", 100)
    m2.inc("gateway_errors_total", 1, status="5xx")
    assert not SLAMonitor(SLAConfig(), m2).evaluate()["gateway_availability"]["ok"]   # 99% < 99.5%
