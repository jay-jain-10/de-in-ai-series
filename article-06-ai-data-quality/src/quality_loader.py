"""
Loads quarantine records (and review decisions) into Snowflake QUALITY schema.

MERGE on (record_id, check_id) -> idempotent; a re-run or a later review decision
updates the same row. dim_quality is built on top of this table (dbt).
"""

import logging
from typing import List

import snowflake.connector

logger = logging.getLogger(__name__)

COLUMNS = [
    "quarantine_id", "record_id", "source_system", "flagged_by_layer", "check_id",
    "check_name", "anomaly_type", "confidence", "severity", "rules_version", "run_id",
    "flagged_timestamp", "reviewer_decision", "reviewed_timestamp", "reviewed_by", "notes",
]

MERGE_SQL = """
MERGE INTO {schema}.quarantine_record AS target
USING (SELECT
    %(quarantine_id)s AS quarantine_id, %(record_id)s AS record_id,
    %(source_system)s AS source_system, %(flagged_by_layer)s AS flagged_by_layer,
    %(check_id)s AS check_id, %(check_name)s AS check_name,
    %(anomaly_type)s AS anomaly_type, %(confidence)s AS confidence,
    %(severity)s AS severity, %(rules_version)s AS rules_version, %(run_id)s AS run_id,
    %(flagged_timestamp)s::TIMESTAMP_NTZ AS flagged_timestamp,
    %(reviewer_decision)s AS reviewer_decision,
    %(reviewed_timestamp)s::TIMESTAMP_NTZ AS reviewed_timestamp,
    %(reviewed_by)s AS reviewed_by, %(notes)s AS notes
) AS source
ON target.record_id = source.record_id AND target.check_id = source.check_id
WHEN MATCHED THEN UPDATE SET
    reviewer_decision = source.reviewer_decision,
    reviewed_timestamp = source.reviewed_timestamp,
    reviewed_by = source.reviewed_by,
    notes = source.notes,
    loaded_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    quarantine_id, record_id, source_system, flagged_by_layer, check_id, check_name,
    anomaly_type, confidence, severity, rules_version, run_id, flagged_timestamp,
    reviewer_decision, reviewed_timestamp, reviewed_by, notes
) VALUES (
    source.quarantine_id, source.record_id, source.source_system, source.flagged_by_layer,
    source.check_id, source.check_name, source.anomaly_type, source.confidence,
    source.severity, source.rules_version, source.run_id, source.flagged_timestamp,
    source.reviewer_decision, source.reviewed_timestamp, source.reviewed_by, source.notes
)
"""


class QualityLoader:
    def __init__(self, account, user, password, database, schema="QUALITY", warehouse="COMPUTE_WH"):
        self.schema = schema
        self.conn = snowflake.connector.connect(
            account=account, user=user, password=password,
            database=database, schema=schema, warehouse=warehouse,
        )

    def load_quarantine_records(self, rows: List[dict]) -> dict:
        loaded, failed = 0, 0
        cur = self.conn.cursor()
        try:
            for row in rows:
                params = {c: row.get(c) for c in COLUMNS}
                try:
                    cur.execute(MERGE_SQL.format(schema=self.schema), params)
                    loaded += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"Failed to load {row.get('record_id')}/{row.get('check_id')}: {e}")
        finally:
            cur.close()
        logger.info(f"Quality load complete: {loaded} loaded, {failed} failed")
        return {"loaded": loaded, "failed": failed}

    def close(self):
        self.conn.close()
