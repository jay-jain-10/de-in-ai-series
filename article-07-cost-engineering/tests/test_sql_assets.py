from pathlib import Path
from unittest.mock import MagicMock
from src.snowflake_loader import load_ddl, create_cost_events_table

ROOT = Path(__file__).resolve().parent.parent
COLS = ["pipeline_name", "team_name", "model", "input_tokens", "output_tokens", "cost_tokens",
        "cost_usd", "cache_hit", "attempt", "is_retry", "latency_ms", "task_type"]


def test_ddl_has_article_columns():
    ddl = load_ddl("RAW")
    assert "RAW.cost_events" in ddl
    for c in COLS:
        assert c in ddl


def test_create_table_executes_with_mock_connection():
    conn = MagicMock()
    create_cost_events_table(conn)
    assert conn.cursor.return_value.execute.call_count == 1


def test_dbt_models_exist_and_use_article_thresholds():
    daily = (ROOT / "dbt_models/marts/fct_daily_cost.sql").read_text()
    assert "0.005" in daily and "200" in daily
    assert "2.0" in (ROOT / "dbt_models/marts/fct_cost_anomalies.sql").read_text()
    assert (ROOT / "dbt_models/staging/stg_cost_events.sql").exists()


def _ddl_columns():
    import re
    ddl = load_ddl("RAW")
    return re.findall(r"^\s{4}(\w+)\s+(?:TIMESTAMP_NTZ|DATE|VARCHAR|NUMBER|FLOAT|BOOLEAN)", ddl, re.M)


def test_python_writers_match_ddl_columns():
    import re
    from src.cost_events import COST_EVENTS_INSERT_SQL, CostEvent
    ddl_cols = set(_ddl_columns()) - {"loaded_at"}
    insert_cols = re.search(r"\(([^)]*)\)\s*VALUES", COST_EVENTS_INSERT_SQL, re.S).group(1)
    insert_cols = {c.strip() for c in insert_cols.split(",")}
    assert insert_cols == ddl_cols
    # every INSERT placeholder is a key emitted by CostEvent.to_dict (Firehose JSON too)
    keys = set(CostEvent("p", "m", 1, 1, 0.1).to_dict())
    assert set(re.findall(r"%\((\w+)\)s", COST_EVENTS_INSERT_SQL)) == ddl_cols <= keys


def test_dbt_staging_reads_only_ddl_columns():
    import re
    stg = (ROOT / "dbt_models/staging/stg_cost_events.sql").read_text()
    body = "\n".join(l.split("--")[0] for l in stg.splitlines())
    select = body.split("FROM")[0]
    referenced = set(re.findall(r"\b([a-z_]+)\b", select)) - {
        "select", "coalesce", "as", "false", "true", "case", "when", "not", "then", "else",
        "end", "unknown", "wasted_cost_usd"}
    assert referenced <= set(_ddl_columns())
    for mart in ("fct_daily_cost", "fct_cost_anomalies", "fct_cost_by_model"):
        assert "ref('stg_cost_events')" in (ROOT / f"dbt_models/marts/{mart}.sql").read_text()
