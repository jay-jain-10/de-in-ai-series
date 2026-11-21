import json
from unittest.mock import MagicMock

import pytest

from src.models import ContractExtraction
from src.schema_evolution import (
    CORE_COLUMNS, columns_for_version, create_table_sql, evolve_table)
from src.snowflake_loader import SnowflakeLoader, build_merge_sql, record_to_row
from tests.conftest import FakeConn


def rec(cid="c1", version=2.1, routing="production", **kw):
    return ContractExtraction(contract_id=cid, extraction_version=version, payment_terms="30 days",
                              routing=routing, confidence_scores={"payment_terms": 0.9}, **kw)


def test_merge_sql_keyed_on_contract_and_version():
    sql = build_merge_sql("RAW", "extracted_contracts", "extracted_contracts_staging")
    assert "ON target.contract_id = source.contract_id\n  AND target.extraction_version = source.extraction_version" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql and "WHEN NOT MATCHED THEN INSERT" in sql
    assert "last_updated = CURRENT_TIMESTAMP()" in sql and "created_timestamp" in sql
    assert "governing_law = source.governing_law" in sql
    # keys are never updated
    assert "contract_id = source.contract_id," not in sql.split("UPDATE SET")[1].split("WHEN NOT")[0]


def test_row_serialises_variants_as_json():
    row = record_to_row(rec())
    assert json.loads(row["confidence_scores"]) == {"payment_terms": 0.9}
    assert row["governing_law"] is None


def test_load_routes_tables_and_commits():
    conn = FakeConn(columns=[c for c in CORE_COLUMNS])
    loader = SnowflakeLoader(connection=conn)
    out = loader.load([rec("a"), rec("b", routing="production_flagged"),
                       rec("c", routing="quarantine", review_required=True, validation_flags=["x:hallucination_suspected"])])
    assert out == {"production": 2, "quarantine": 1, "review_queue": 1}
    assert conn.committed and not conn.rolled_back
    sqls = [e[1] for e in conn.log if e[0] == "execute"]
    merges = [s for s in sqls if s.startswith("MERGE INTO RAW.extracted_contracts ")]
    assert len(merges) == 1
    assert any(s.startswith("MERGE INTO RAW.extracted_contracts_quarantine") for s in sqls)
    inserts = [e for e in conn.log if e[0] == "executemany" and e[1].startswith("INSERT")]
    assert len(inserts[0][2]) == 2 and len(inserts[1][2]) == 1


def test_rerouted_keys_are_deleted_from_the_other_table():
    """Same (contract_id, version) re-run with a different route must not leave a stale row."""
    conn = FakeConn(columns=list(CORE_COLUMNS))
    SnowflakeLoader(connection=conn).load([rec("a"), rec("q", routing="quarantine")])
    deletes = {e[1].split()[2]: e[2] for e in conn.log
               if e[0] == "executemany" and e[1].startswith("DELETE")}
    assert deletes["RAW.extracted_contracts_quarantine"] == [{"contract_id": "a", "extraction_version": 2.1}]
    assert deletes["RAW.extracted_contracts"] == [{"contract_id": "q", "extraction_version": 2.1}]


def test_all_ddl_runs_before_the_transaction_begins():
    """Snowflake DDL auto-commits; any DDL after BEGIN would silently commit partial DML."""
    conn = FakeConn(columns=list(CORE_COLUMNS))
    SnowflakeLoader(connection=conn).load([rec("a"), rec("q", routing="quarantine")])
    stmts = [e[1].lstrip().split()[0].upper() for e in conn.log]
    begin = stmts.index("BEGIN")
    assert not {"CREATE", "ALTER"} & set(stmts[begin:])
    assert {"INSERT", "MERGE", "DELETE"} <= set(stmts[begin:])
    assert not {"INSERT", "MERGE", "DELETE"} & set(stmts[:begin])


def test_duplicate_keys_in_batch_collapse_last_wins():
    conn = FakeConn(columns=list(CORE_COLUMNS))
    SnowflakeLoader(connection=conn).load([rec("a"), rec("a")])
    rows = [e for e in conn.log if e[0] == "executemany" and e[1].startswith("INSERT")][0][2]
    assert len(rows) == 1 and rows[0]["contract_id"] == "a"


def test_same_key_twice_is_idempotent_statement_sequence():
    c1, c2 = FakeConn(columns=list(CORE_COLUMNS)), FakeConn(columns=list(CORE_COLUMNS))
    SnowflakeLoader(connection=c1).load([rec()])
    SnowflakeLoader(connection=c2).load([rec()])
    assert [e[1] for e in c1.log if e[0] == "execute"] == [e[1] for e in c2.log if e[0] == "execute"]


def test_new_version_is_distinct_key():
    conn = FakeConn(columns=list(CORE_COLUMNS))
    SnowflakeLoader(connection=conn).load([rec(version=1.5), rec(version=2.0)])
    rows = [e for e in conn.log if e[0] == "executemany" and e[1].startswith("INSERT")][0][2]
    assert sorted(r["extraction_version"] for r in rows) == [1.5, 2.0]


def test_failure_rolls_back():
    conn = FakeConn(columns=list(CORE_COLUMNS))
    orig = conn.cursor

    def bad_cursor():
        cur = orig()
        cur.executemany = MagicMock(side_effect=RuntimeError("sf down"))
        return cur
    conn.cursor = bad_cursor
    with pytest.raises(RuntimeError):
        SnowflakeLoader(connection=conn).load([rec()])
    assert conn.rolled_back and not conn.committed


def test_backfill_candidates_query():
    conn = FakeConn()
    cur = conn.cursor()
    cur.fetchall = lambda: [("c1",), ("c2",)]
    conn.cursor = lambda: cur
    ids = SnowflakeLoader(connection=conn).backfill_candidates(2.0, "2024-01-01")
    assert ids == ["c1", "c2"]
    sql, params = conn.log[-1][1], conn.log[-1][2]
    assert "extraction_version < %(max_version)s" in sql and params["max_version"] == 2.0


# -- schema evolution --------------------------------------------------------------

def test_governing_law_only_in_v2_columns():
    assert "governing_law" not in columns_for_version(1.5)
    assert "governing_law" in columns_for_version(2.0)
    assert "governing_law" not in create_table_sql("RAW", version=1.0)


def test_evolve_adds_only_missing_columns():
    conn = FakeConn(columns=[c for c in CORE_COLUMNS if c != "governing_law"])
    added = evolve_table(conn.cursor(), "RAW")
    assert added == ["governing_law"]
    assert any("ADD COLUMN IF NOT EXISTS governing_law" in e[1] for e in conn.log)


def test_evolve_noop_when_current():
    conn = FakeConn(columns=list(CORE_COLUMNS))
    assert evolve_table(conn.cursor(), "RAW") == []


def test_v1_record_without_governing_law_still_valid():
    r = ContractExtraction(contract_id="old", extraction_version=1.5, payment_terms="30 days")
    assert r.governing_law is None
