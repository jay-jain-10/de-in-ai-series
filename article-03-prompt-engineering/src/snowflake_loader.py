"""Snowflake loader with prompt_version lineage and exactly-once reprocessing.

The MERGE key is (input_id, prompt_name, prompt_version). Re-running the same
input through a new prompt version inserts a new row; replaying the same
version updates in place. No duplicates either way. prompt_name is part of the
key because the table is shared by every prompt: without it, e.g.
escalation_classifier v1.3 would overwrite sentiment_classifier v1.3 for the
same input_id.
"""

import json
import logging
from typing import List

import snowflake.connector

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.prompt_classifications (
    input_id VARCHAR(100) NOT NULL,
    prompt_version VARCHAR(20) NOT NULL,
    prompt_name VARCHAR(100) NOT NULL,
    output_json VARCHAR(4000),
    confidence FLOAT,
    schema_valid BOOLEAN,
    model_used VARCHAR(100),
    classified_at TIMESTAMP_NTZ,
    loaded_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (input_id, prompt_name, prompt_version)
);
"""

UPSERT_SQL = """
MERGE INTO {schema}.prompt_classifications AS target
USING (SELECT
    %(input_id)s AS input_id,
    %(prompt_version)s AS prompt_version,
    %(prompt_name)s AS prompt_name,
    %(output_json)s AS output_json,
    %(confidence)s AS confidence,
    %(schema_valid)s AS schema_valid,
    %(model_used)s AS model_used,
    %(classified_at)s::TIMESTAMP_NTZ AS classified_at
) AS source
ON target.input_id = source.input_id
  AND target.prompt_name = source.prompt_name
  AND target.prompt_version = source.prompt_version
WHEN MATCHED THEN UPDATE SET
    output_json = source.output_json,
    confidence = source.confidence,
    schema_valid = source.schema_valid,
    model_used = source.model_used,
    classified_at = source.classified_at,
    loaded_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    input_id, prompt_version, prompt_name, output_json, confidence,
    schema_valid, model_used, classified_at
) VALUES (
    source.input_id, source.prompt_version, source.prompt_name, source.output_json,
    source.confidence, source.schema_valid, source.model_used, source.classified_at
);
"""

# Inputs not yet processed by a given prompt version (selective reprocessing).
REPROCESS_SQL = """
SELECT DISTINCT old.input_id
FROM {schema}.prompt_classifications old
WHERE old.prompt_name = %(prompt_name)s
  AND NOT EXISTS (
    SELECT 1 FROM {schema}.prompt_classifications new
    WHERE new.input_id = old.input_id
      AND new.prompt_name = old.prompt_name
      AND new.prompt_version = %(new_version)s
  );
"""

LINEAGE_SQL = """
SELECT prompt_version, COUNT(*) AS row_count, MIN(classified_at) AS first_seen, MAX(classified_at) AS last_seen
FROM {schema}.prompt_classifications
WHERE prompt_name = %(prompt_name)s
GROUP BY prompt_version
ORDER BY prompt_version;
"""


def to_row(result, input_id: str, classified_at: str) -> dict:
    """Convert a RunResult to a loadable row, stamped with its prompt_version."""
    return {
        "input_id": input_id,
        "prompt_version": result.prompt_version,
        "prompt_name": result.prompt_name,
        "output_json": json.dumps(result.output.model_dump()) if result.output else None,
        "confidence": result.confidence,
        "schema_valid": result.schema_valid,
        "model_used": result.model,
        "classified_at": classified_at,
    }


class SnowflakeLoader:
    def __init__(self, account: str, user: str, password: str, database: str,
                 schema: str = "RAW", warehouse: str = "COMPUTE_WH"):
        self.schema = schema
        self.conn = snowflake.connector.connect(
            account=account, user=user, password=password,
            database=database, schema=schema, warehouse=warehouse,
        )

    def ensure_table(self) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(CREATE_TABLE_SQL.format(schema=self.schema))
        finally:
            cur.close()

    def load(self, rows: List[dict]) -> int:
        cur = self.conn.cursor()
        try:
            # Snowflake sessions autocommit by default; without an explicit BEGIN
            # each MERGE commits on its own and rollback() below would be a no-op.
            cur.execute("BEGIN")
            for row in rows:
                cur.execute(UPSERT_SQL.format(schema=self.schema), row)
            self.conn.commit()
            return len(rows)
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def inputs_to_reprocess(self, prompt_name: str, new_version: str) -> List[str]:
        cur = self.conn.cursor()
        try:
            cur.execute(REPROCESS_SQL.format(schema=self.schema),
                        {"prompt_name": prompt_name, "new_version": new_version})
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def lineage(self, prompt_name: str) -> list:
        cur = self.conn.cursor()
        try:
            cur.execute(LINEAGE_SQL.format(schema=self.schema), {"prompt_name": prompt_name})
            return cur.fetchall()
        finally:
            cur.close()
