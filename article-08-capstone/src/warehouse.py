"""Warehouse writer: idempotent MERGE into the five article tables (+ lineage)."""

import json
import re
from typing import Any, Optional, Protocol

TABLE_KEYS = {
    "raw_documents": ["document_id"],
    "extracted_claims": ["document_id", "pipeline"],
    "validation_results": ["document_id", "layer"],
    "fraud_scores": ["document_id"],
    "cost_events": ["event_id"],
    "lineage": ["document_id", "step", "timestamp"],
}

DDL = """
CREATE TABLE IF NOT EXISTS raw_documents (document_id STRING, team STRING, pipeline STRING, source_type STRING,
  source_id STRING, received_at STRING, format STRING, text STRING, extraction_confidence FLOAT, page_count INT);
CREATE TABLE IF NOT EXISTS extracted_claims (document_id STRING, pipeline STRING, team STRING, schema_version STRING,
  fields STRING, extraction_confidence FLOAT, risk_class STRING, layer2_status STRING, processing_version STRING,
  loaded_at STRING);
CREATE TABLE IF NOT EXISTS validation_results (document_id STRING, layer INT, passed BOOLEAN, issues STRING,
  sampled BOOLEAN);
CREATE TABLE IF NOT EXISTS fraud_scores (document_id STRING, score FLOAT, rationale STRING, model STRING);
CREATE TABLE IF NOT EXISTS cost_events (event_id STRING, called_at STRING, team STRING, pipeline STRING,
  task_type STRING, model STRING, input_tokens INT, output_tokens INT, cost_usd FLOAT, cached BOOLEAN);
CREATE TABLE IF NOT EXISTS lineage (document_id STRING, step STRING, timestamp STRING, processing_version STRING,
  model_used STRING, token_count INT, cost_usd FLOAT, confidence_score FLOAT, details STRING);
""".strip()

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Warehouse(Protocol):
    def merge(self, table: str, row: dict) -> None: ...
    def get(self, table: str, **key) -> Optional[dict]: ...
    def rows(self, table: str) -> list[dict]: ...


class InMemoryWarehouse:
    def __init__(self):
        self.tables: dict[str, dict[tuple, dict]] = {t: {} for t in TABLE_KEYS}

    def _key(self, table: str, row: dict) -> tuple:
        return tuple(str(row[k]) for k in TABLE_KEYS[table])

    def merge(self, table: str, row: dict) -> None:
        self.tables[table][self._key(table, row)] = dict(row)   # upsert => idempotent

    def get(self, table: str, **key) -> Optional[dict]:
        for row in self.tables[table].values():
            if all(row.get(k) == v for k, v in key.items()):
                return row
        return None

    def rows(self, table: str) -> list[dict]:
        return list(self.tables[table].values())


class SnowflakeWarehouse:
    """MERGE loader over a snowflake.connector connection (same pattern as Article 1)."""

    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _ser(v: Any) -> Any:
        return json.dumps(v, default=str) if isinstance(v, (dict, list)) else v

    def merge(self, table: str, row: dict) -> None:
        if table not in TABLE_KEYS or not all(_IDENT.match(c) for c in row):
            raise ValueError("invalid table or column name")
        cols, keys = list(row), TABLE_KEYS[table]
        src = ", ".join(f"%({c})s AS {c}" for c in cols)
        on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
        sets = ", ".join(f"t.{c} = s.{c}" for c in cols if c not in keys)
        sql = (f"MERGE INTO {table} t USING (SELECT {src}) s ON {on} "
               f"WHEN MATCHED THEN UPDATE SET {sets} "
               f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) VALUES ({', '.join('s.' + c for c in cols)})")
        cur = self._conn.cursor()
        try:
            cur.execute(sql, {c: self._ser(v) for c, v in row.items()})
        finally:
            cur.close()

    def get(self, table: str, **key) -> Optional[dict]:
        if table not in TABLE_KEYS or not all(_IDENT.match(c) for c in key):
            raise ValueError("invalid table or column name")
        where = " AND ".join(f"{k} = %({k})s" for k in key)
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1", key)
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d[0].lower() for d in cur.description], row))
        finally:
            cur.close()

    def rows(self, table: str) -> list[dict]:
        if table not in TABLE_KEYS:
            raise ValueError("invalid table")
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {table}")
            names = [d[0].lower() for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        finally:
            cur.close()
