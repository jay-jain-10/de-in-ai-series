from unittest.mock import MagicMock, patch

import pytest

from src import snowflake_loader as sl
from src.output_contracts import SentimentOutput
from src.prompt_runner import RunResult


def make_loader():
    conn = MagicMock()
    with patch("src.snowflake_loader.snowflake.connector.connect", return_value=conn) as c:
        loader = sl.SnowflakeLoader("acct", "u", "p", "DB", "RAW", "WH")
    return loader, conn


def result(version="1.3"):
    return RunResult("sentiment_classifier", version, "claude-haiku-4-5-20251001", "t",
                     output=SentimentOutput(sentiment="neutral", confidence=0.7), schema_valid=True)


def test_table_has_prompt_version_column_and_composite_key():
    assert "prompt_version VARCHAR" in sl.CREATE_TABLE_SQL
    # shared table: prompt_name must be in the key or prompts with the same
    # version string overwrite each other for the same input
    assert "PRIMARY KEY (input_id, prompt_name, prompt_version)" in sl.CREATE_TABLE_SQL


def _on_clause(sql):
    import re
    on = sql.split("ON ", 1)[1].split("WHEN", 1)[0]
    return set(re.findall(r"target\.(\w+) = source\.(\w+)", on))


def test_merge_matches_on_input_name_and_version():
    assert _on_clause(sl.UPSERT_SQL) == {("input_id", "input_id"), ("prompt_name", "prompt_name"),
                                         ("prompt_version", "prompt_version")}
    # key columns are never rewritten on update
    update = sl.UPSERT_SQL.split("UPDATE SET", 1)[1].split("WHEN NOT MATCHED", 1)[0]
    assert "prompt_name =" not in update and "prompt_version =" not in update


def test_same_version_string_on_two_prompts_does_not_collide():
    sent = sl.to_row(result("1.3"), "T1", "x")
    esc = sl.to_row(RunResult("escalation_classifier", "1.3", "m", "t", output=None), "T1", "x")
    key_cols = [c for c, _ in _on_clause(sl.UPSERT_SQL)]
    assert tuple(sent[c] for c in key_cols) != tuple(esc[c] for c in key_cols)


def test_reprocess_query_scoped_to_prompt_name():
    sub = sl.REPROCESS_SQL.split("NOT EXISTS", 1)[1]
    assert "new.prompt_name = old.prompt_name" in sub


def test_to_row_stamps_prompt_version():
    row = sl.to_row(result("1.3"), "T1", "2026-09-01T00:00:00")
    assert row["prompt_version"] == "1.3" and row["input_id"] == "T1" and row["schema_valid"] is True
    assert '"sentiment": "neutral"' in row["output_json"]


def test_to_row_for_invalid_output_has_null_json():
    r = RunResult("p", "1.0", "m", "t", output=None, schema_valid=False)
    row = sl.to_row(r, "T1", "2026-09-01T00:00:00")
    assert row["output_json"] is None and row["schema_valid"] is False


def test_load_executes_merge_per_row_and_commits():
    loader, conn = make_loader()
    rows = [sl.to_row(result("1.3"), f"T{i}", "2026-09-01T00:00:00") for i in range(3)]
    assert loader.load(rows) == 3
    cur = conn.cursor.return_value
    stmts = [c.args[0].strip() for c in cur.execute.call_args_list]
    # explicit transaction (Snowflake autocommits otherwise, making rollback a no-op)
    assert stmts[0] == "BEGIN" and len(stmts) == 4
    assert all("MERGE INTO RAW.prompt_classifications" in s for s in stmts[1:])
    assert [c.args[1]["input_id"] for c in cur.execute.call_args_list[1:]] == ["T0", "T1", "T2"]
    conn.commit.assert_called_once()


def test_load_rolls_back_on_error():
    loader, conn = make_loader()
    conn.cursor.return_value.execute.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        loader.load([sl.to_row(result(), "T1", "2026-09-01T00:00:00")])
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_reprocessing_new_version_uses_distinct_keys():
    # same input, two versions => distinct MERGE keys (checked against the real ON clause)
    key_cols = [c for c, _ in _on_clause(sl.UPSERT_SQL)]
    a = sl.to_row(result("1.3"), "T1", "x")
    b = sl.to_row(result("1.4"), "T1", "x")
    assert tuple(a[c] for c in key_cols) != tuple(b[c] for c in key_cols)
    # and replaying the same version produces the same key (update in place)
    assert tuple(a[c] for c in key_cols) == tuple(sl.to_row(result("1.3"), "T1", "y")[c] for c in key_cols)


def test_inputs_to_reprocess_and_lineage_queries():
    loader, conn = make_loader()
    cur = conn.cursor.return_value
    cur.fetchall.return_value = [("T1",), ("T2",)]
    assert loader.inputs_to_reprocess("sentiment_classifier", "1.4") == ["T1", "T2"]
    assert cur.execute.call_args.args[1] == {"prompt_name": "sentiment_classifier", "new_version": "1.4"}
    cur.fetchall.return_value = [("1.2", 5, None, None), ("1.3", 9, None, None)]
    assert loader.lineage("sentiment_classifier")[1][0] == "1.3"
