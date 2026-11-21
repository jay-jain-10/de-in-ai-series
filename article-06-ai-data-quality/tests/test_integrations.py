"""S3, Snowflake and Airflow wiring with everything mocked."""

import json
import os
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest


# ---- S3 ----

def test_load_claims_from_s3(monkeypatch):
    import src.s3_io as s3io
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "claims/d/a.json"}, {"Key": "claims/d/_SUCCESS"}]}, {}]
    client.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps([{"claim_id": "1"}, {"claim_id": "2"}]).encode())}
    monkeypatch.setattr(s3io.boto3, "client", lambda *a, **k: client)
    assert len(s3io.load_claims_from_s3("b", "claims/d/")) == 2
    client.get_object.assert_called_once()   # non-json key skipped
    s3io.write_json_to_s3("b", "k", {"a": 1})
    client.put_object.assert_called_once()


def test_load_claims_from_s3_ndjson(monkeypatch):
    import src.s3_io as s3io
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "c/a.json"}]}]
    body = '{"claim_id": "1"}\n{"claim_id": "2"}\n\n{"claim_id": "3"}\n'
    client.get_object.return_value = {"Body": MagicMock(read=lambda: body.encode())}
    monkeypatch.setattr(s3io.boto3, "client", lambda *a, **k: client)
    assert [c["claim_id"] for c in s3io.load_claims_from_s3("b", "c/")] == ["1", "2", "3"]


# ---- Snowflake ----

def test_quality_loader_merge_is_idempotent_sql(monkeypatch, store):
    import src.quality_loader as ql
    from src.quarantine import QuarantineRecord
    conn = MagicMock()
    monkeypatch.setattr(ql.snowflake.connector, "connect", lambda **k: conn)
    store.add(QuarantineRecord("A", "s", "L2", "L2:semantic_consistency", "semantic_consistency",
                               anomaly_type="m", confidence=0.9))
    loader = ql.QualityLoader("acct", "u", "p", "DB")
    out = loader.load_quarantine_records(store.all())
    assert out == {"loaded": 1, "failed": 0}
    sql, params = conn.cursor.return_value.execute.call_args[0]
    assert "MERGE INTO QUALITY.quarantine_record" in sql
    assert "ON target.record_id = source.record_id AND target.check_id = source.check_id" in sql
    assert params["record_id"] == "A" and params["check_id"] == "L2:semantic_consistency"
    loader.close()
    conn.close.assert_called_once()


def test_quality_loader_counts_failures(monkeypatch):
    import src.quality_loader as ql
    conn = MagicMock()
    conn.cursor.return_value.execute.side_effect = RuntimeError("boom")
    monkeypatch.setattr(ql.snowflake.connector, "connect", lambda **k: conn)
    loader = ql.QualityLoader("a", "u", "p", "DB")
    assert loader.load_quarantine_records([{"record_id": "x", "check_id": "y"}]) == {"loaded": 0, "failed": 1}


# ---- SQL / dbt artefacts ----

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    with open(os.path.join(ROOT, *p)) as f:
        return f.read()


def test_ddl_matches_article_quarantine_schema():
    ddl = read("sql", "quarantine_record.sql")
    for col in ["record_id", "source_system", "flagged_by_layer", "check_name", "anomaly_type",
                "confidence", "flagged_timestamp", "reviewer_decision", "reviewed_timestamp",
                "reviewed_by", "notes", "UNIQUE (record_id, check_id)"]:
        assert col in ddl
    assert "fix_upstream" in ddl and "accept_as_is" in ddl and "confirm_error" in ddl


def test_dbt_models_exist_and_reference_each_other():
    assert "ref('stg_quarantine_records')" in read("dbt_models", "marts", "dim_quality.sql")
    assert "ref('dim_quality')" in read("dbt_models", "marts", "fct_source_quality.sql")
    q = read("dbt_models", "marts", "fct_source_quality.sql")
    assert "error_rate_pct" in q and "NULLIF" in q
    assert "0.8" in read("dbt_models", "staging", "source_risk_score.sql")


def test_loader_columns_exist_in_snowflake_ddl_and_dbt_staging():
    import re
    from src.quality_loader import COLUMNS, MERGE_SQL
    ddl = read("sql", "dim_quality.sql")
    table = ddl.split("CREATE OR REPLACE VIEW")[0]
    sf_cols = set(re.findall(r"^\s{4}(\w+)\s+(?:VARCHAR|FLOAT|TIMESTAMP_NTZ)", table, re.M))
    assert set(COLUMNS) <= sf_cols
    assert "loaded_at" in sf_cols                    # MERGE updates it
    for c in COLUMNS:
        assert f"%({c})s" in MERGE_SQL
    stg = read("dbt_models", "staging", "stg_quarantine_records.sql")
    select = stg.split("FROM")[0]
    selected = {c for c in COLUMNS if re.search(rf"\b{c}\b", select)}
    assert selected == set(COLUMNS) - {"notes"}      # staging drops only free-text notes
    code = "\n".join(l.split("--")[0] for l in stg.splitlines())
    assert "CURRENT_TIMESTAMP" not in code and "SYSDATE()" in code   # UTC NTZ comparisons


def test_sqlite_ddl_and_postgres_ddl_have_same_columns():
    from src.quarantine import SCHEMA
    import re
    pg = read("sql", "quarantine_record.sql")
    pg_cols = set(re.findall(r"^\s{4}(\w+)\s+(?:UUID|VARCHAR|DOUBLE|TIMESTAMP|TEXT|JSONB)", pg, re.M))
    lite_cols = set(re.findall(r"^\s{4}(\w+)\s+(?:TEXT|REAL)", SCHEMA, re.M))
    assert pg_cols == lite_cols


# ---- Airflow DAG (airflow is stubbed; only wiring is verified) ----

@pytest.fixture
def dag_module(monkeypatch, tmp_path):
    registry = []

    class FakeDAG:
        def __init__(self, dag_id, **kw):
            self.dag_id, self.kw, self.tasks = dag_id, kw, {}
            registry.append(self)
        def __enter__(self):
            FakeDAG.current = self
            return self
        def __exit__(self, *a):
            return False

    class FakeOp:
        def __init__(self, task_id, **kw):
            self.task_id, self.kw, self.down = task_id, kw, []
            FakeDAG.current.tasks[task_id] = self
        def __rshift__(self, other):
            for o in (other if isinstance(other, list) else [other]):
                self.down.append(o.task_id if hasattr(o, "task_id") else o)
            return other

    af = types.ModuleType("airflow"); af.DAG = FakeDAG
    ops = types.ModuleType("airflow.operators")
    py = types.ModuleType("airflow.operators.python"); py.PythonOperator = FakeOp
    bash = types.ModuleType("airflow.operators.bash"); bash.BashOperator = FakeOp
    for n, m in {"airflow": af, "airflow.operators": ops,
                 "airflow.operators.python": py, "airflow.operators.bash": bash}.items():
        monkeypatch.setitem(sys.modules, n, m)
    monkeypatch.setenv("QUALITY_STAGING_DIR", str(tmp_path / "stage"))
    monkeypatch.setenv("QUARANTINE_DB_PATH", str(tmp_path / "q.db"))
    monkeypatch.setenv("RULES_FILE", os.path.join(ROOT, "rules", "healthcare_claims_v1.yaml"))
    sys.modules.pop("dags.healthcare_quality_dag", None)
    import importlib.util
    spec = importlib.util.spec_from_file_location("hq_dag", os.path.join(ROOT, "dags", "healthcare_quality_dag.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.registry = registry
    return mod


def test_dag_structure(dag_module):
    dags = {d.dag_id: d for d in dag_module.registry}
    main = dags["healthcare_data_quality_pipeline"]
    assert set(main.tasks) == {"ingest", "layer1_validate", "sample_for_layer2", "semantic_validate",
                               "persist_to_snowflake", "run_dbt", "check_review_sla", "log_metrics"}
    t = main.tasks
    assert t["ingest"].down == ["layer1_validate"]
    assert t["layer1_validate"].down == ["sample_for_layer2"]
    assert t["sample_for_layer2"].down == ["semantic_validate"]
    assert t["semantic_validate"].down == ["persist_to_snowflake"]
    assert t["persist_to_snowflake"].down == ["run_dbt"]
    assert set(t["run_dbt"].down) == {"check_review_sla", "log_metrics"}
    assert main.kw["schedule_interval"] == "@daily" and main.kw["catchup"] is False
    assert main.kw["default_args"]["retries"] == 2
    assert t["layer1_validate"].kw["execution_timeout"] == timedelta(minutes=10)
    assert t["semantic_validate"].kw["execution_timeout"] == timedelta(hours=2)
    assert dags["healthcare_quality_monthly_calibration"].kw["schedule_interval"] == "@monthly"


def test_dag_tasks_run_end_to_end_and_are_retry_safe(dag_module, monkeypatch, patients, fake_anthropic):
    from src.data_generator import make_claims
    claims, truth = make_claims(n=120, seed=3, patients=patients)
    import src.s3_io as s3io
    monkeypatch.setattr(s3io, "load_claims_from_s3",
                        lambda b, p, region="us-east-1": claims if p.startswith("claims/") else [{"patient_id": x} for x in patients])
    monkeypatch.setenv("CLAIMS_S3_BUCKET", "b")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    class TI:
        def __init__(self): self.x = {}
        def xcom_push(self, key, value): self.x[key] = value
        def xcom_pull(self, key, task_ids=None): return self.x.get(key)

    ti = TI()
    ctx = {"ds": "2026-06-01", "ti": ti}
    dag_module.ingest_from_s3(**ctx)
    dag_module.layer1_validate(**ctx)
    dag_module.sample_for_layer2(**ctx)
    assert ti.x["sample_summary"]["tier"] == "high"
    dag_module.semantic_validate(**ctx)
    store = dag_module._store()
    n = store.count()
    expected = sum(1 for v in truth.values() if v != "ok")
    assert n >= expected                     # >= : one bad row can fail several L1 checks
    # Airflow retry of every task: no duplicate quarantine rows
    for fn in (dag_module.layer1_validate, dag_module.sample_for_layer2, dag_module.semantic_validate):
        fn(**ctx)
    assert dag_module._store().count() == n
    dag_module.check_review_sla(**ctx)
    assert ti.x["review_sla_breaches"] == 0
    dag_module.log_pipeline_metrics(**ctx)
    dag_module.draw_monthly_calibration_sample(**ctx)
    assert ti.x["calibration_sample_size"] == sum(1 for r in store.all() if r["flagged_by_layer"] == "L2")


def test_dag_persist_skips_when_empty_and_loads_when_not(dag_module, monkeypatch):
    import src.quality_loader as ql
    loader = MagicMock()
    monkeypatch.setattr(ql, "QualityLoader", lambda **k: loader)
    for k in ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_DATABASE", "SNOWFLAKE_WAREHOUSE"]:
        monkeypatch.setenv(k, "x")
    ti = MagicMock()
    dag_module.persist_to_snowflake(ti=ti, ds="2026-06-01")
    loader.load_quarantine_records.assert_not_called()
    from src.quarantine import QuarantineRecord
    dag_module._store().add(QuarantineRecord("A", "s", "L1", "L1:x", "x"))
    loader.load_quarantine_records.return_value = {"loaded": 1, "failed": 0}
    dag_module.persist_to_snowflake(ti=ti, ds="2026-06-01")
    loader.load_quarantine_records.assert_called_once()
    loader.close.assert_called_once()
    # Partial load failure must fail the task (so Airflow retries), not be swallowed.
    loader.load_quarantine_records.return_value = {"loaded": 0, "failed": 1}
    with pytest.raises(RuntimeError):
        dag_module.persist_to_snowflake(ti=ti, ds="2026-06-01")
    assert loader.close.call_count == 2
