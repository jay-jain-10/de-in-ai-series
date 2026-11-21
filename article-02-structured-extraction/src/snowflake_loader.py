"""Snowflake loader: idempotent MERGE on (contract_id, extraction_version).

Re-running the pipeline, or reprocessing with the same extraction_version,
updates rows in place instead of duplicating them. A new extraction_version
produces a new row, preserving a per-version audit trail.
"""

import json
import logging
from typing import Dict, List, Optional

import snowflake.connector

from src.config import PipelineConfig, SnowflakeConfig
from src.models import ContractExtraction
from src.schema_evolution import (
    KEY_COLUMNS, VARIANT_COLUMNS, all_data_columns, create_table_sql, evolve_table)

logger = logging.getLogger(__name__)

PRODUCTION_TABLE = "extracted_contracts"
QUARANTINE_TABLE = "extracted_contracts_quarantine"
REVIEW_TABLE = "human_review_queue"

CREATE_REVIEW_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.human_review_queue (
    contract_id VARCHAR(100),
    extraction_version FLOAT,
    reason VARCHAR(1000),
    sla_due TIMESTAMP_NTZ,
    status VARCHAR(20) DEFAULT 'open',
    created_timestamp TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (contract_id, extraction_version)
);
"""

MERGE_REVIEW_SQL = """
MERGE INTO {schema}.human_review_queue AS target
USING (SELECT %(contract_id)s AS contract_id, %(extraction_version)s AS extraction_version,
              %(reason)s AS reason) AS source
ON target.contract_id = source.contract_id
  AND target.extraction_version = source.extraction_version
WHEN MATCHED THEN UPDATE SET reason = source.reason
WHEN NOT MATCHED THEN INSERT (contract_id, extraction_version, reason, sla_due)
  VALUES (source.contract_id, source.extraction_version, source.reason,
          DATEADD(hour, 24, CURRENT_TIMESTAMP()));
"""

BACKFILL_SQL = """
SELECT contract_id
FROM {schema}.extracted_contracts
WHERE extraction_version < %(max_version)s
  AND contract_date > %(since)s
"""


def build_merge_sql(schema: str, table: str, staging: str, columns: Optional[List[str]] = None) -> str:
    """MERGE statement keyed on (contract_id, extraction_version)."""
    cols = columns or all_data_columns()
    on = "\n  AND ".join(f"target.{k} = source.{k}" for k in KEY_COLUMNS)
    updates = ",\n    ".join([f"{c} = source.{c}" for c in cols] + ["last_updated = CURRENT_TIMESTAMP()"])
    insert_cols = ", ".join(KEY_COLUMNS + cols + ["created_timestamp"])
    insert_vals = ", ".join([f"source.{c}" for c in KEY_COLUMNS + cols] + ["CURRENT_TIMESTAMP()"])
    return (
        f"MERGE INTO {schema}.{table} AS target\n"
        f"USING {schema}.{staging} AS source\nON {on}\n"
        f"WHEN MATCHED THEN UPDATE SET\n    {updates}\n"
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols})\nVALUES ({insert_vals});"
    )


def record_to_row(rec: ContractExtraction) -> Dict:
    row = rec.model_dump()
    for c in VARIANT_COLUMNS:
        row[c] = json.dumps(row[c])
    return {k: row.get(k) for k in KEY_COLUMNS + all_data_columns()}


class SnowflakeLoader:
    def __init__(self, config: SnowflakeConfig = None, connection=None):
        self.config = config or SnowflakeConfig()
        self._conn = connection

    def _connect(self):
        if self._conn is None:
            self._conn = snowflake.connector.connect(
                account=self.config.account, user=self.config.user,
                password=self.config.password, database=self.config.database,
                schema=self.config.schema, warehouse=self.config.warehouse)
        return self._conn

    @staticmethod
    def _staging(table: str) -> str:
        return f"{table}_staging"

    def _create_staging(self, cur, table: str) -> None:
        schema = self.config.schema
        cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{self._staging(table)} "
                    f"LIKE {schema}.{table}")

    def _merge_rows(self, cur, table: str, rows: List[Dict]) -> None:
        schema = self.config.schema
        staging = self._staging(table)
        cols = KEY_COLUMNS + all_data_columns()
        placeholders = ", ".join(
            f"PARSE_JSON(%({c})s)" if c in VARIANT_COLUMNS else f"%({c})s" for c in cols)
        # Snowflake does not allow PARSE_JSON in VALUES; INSERT ... SELECT works.
        cur.executemany(
            f"INSERT INTO {schema}.{staging} ({', '.join(cols)}) SELECT {placeholders}", rows)
        cur.execute(build_merge_sql(schema, table, staging))

    def _delete_keys(self, cur, table: str, records: List[ContractExtraction]) -> None:
        """Remove (contract_id, extraction_version) rows that were re-routed to the other table.

        Without this, re-running a contract whose routing changed (e.g. production ->
        quarantine after a validation fix) leaves the stale row behind and it keeps
        being served from production.
        """
        cur.executemany(
            f"DELETE FROM {self.config.schema}.{table} "
            "WHERE contract_id = %(contract_id)s AND extraction_version = %(extraction_version)s",
            [{"contract_id": r.contract_id, "extraction_version": r.extraction_version} for r in records])

    def load(self, records: List[ContractExtraction]) -> Dict[str, int]:
        """Route records to production / quarantine tables via MERGE.

        Duplicate keys inside a batch are collapsed (last wins) so MERGE is
        deterministic. All DDL (which auto-commits in Snowflake) runs first;
        the DML then runs inside an explicit transaction so a failure leaves
        neither table partially updated.
        """
        deduped = {(r.contract_id, r.extraction_version): r for r in records}
        prod = [r for r in deduped.values() if r.routing in ("production", "production_flagged")]
        quar = [r for r in deduped.values() if r.routing == "quarantine"]
        review = [r for r in deduped.values() if r.review_required]

        conn = self._connect()
        cur = conn.cursor()
        schema = self.config.schema
        try:
            # --- DDL (implicitly commits; must not be interleaved with DML) ---
            for table in (PRODUCTION_TABLE, QUARANTINE_TABLE):
                cur.execute(create_table_sql(schema, table))
                evolve_table(cur, schema, table)
            cur.execute(CREATE_REVIEW_QUEUE_SQL.format(schema=schema))
            if prod:
                self._create_staging(cur, PRODUCTION_TABLE)
            if quar:
                self._create_staging(cur, QUARANTINE_TABLE)

            # --- DML in one transaction ---
            cur.execute("BEGIN")
            if prod:
                self._merge_rows(cur, PRODUCTION_TABLE, [record_to_row(r) for r in prod])
                self._delete_keys(cur, QUARANTINE_TABLE, prod)
            if quar:
                self._merge_rows(cur, QUARANTINE_TABLE, [record_to_row(r) for r in quar])
                self._delete_keys(cur, PRODUCTION_TABLE, quar)
            for r in review:
                reason = ", ".join(r.validation_flags) or f"routing={r.routing}"
                cur.execute(MERGE_REVIEW_SQL.format(schema=schema), {
                    "contract_id": r.contract_id, "extraction_version": r.extraction_version,
                    "reason": reason[:1000]})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
        return {"production": len(prod), "quarantine": len(quar), "review_queue": len(review)}

    def backfill_candidates(self, max_version: float, since: str) -> List[str]:
        """contract_ids still on an older extraction_version (selective reprocessing)."""
        cur = self._connect().cursor()
        try:
            cur.execute(BACKFILL_SQL.format(schema=self.config.schema),
                        {"max_version": max_version, "since": since})
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
