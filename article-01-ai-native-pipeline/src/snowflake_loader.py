"""
Snowflake loader with idempotent MERGE operations.

Uses MERGE (upsert) to handle pipeline reruns without creating duplicates.
"""

import snowflake.connector
import logging
from typing import List

logger = logging.getLogger(__name__)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.support_ticket_classifications (
    ticket_id VARCHAR(50) PRIMARY KEY,
    created_at TIMESTAMP_NTZ,
    customer_id VARCHAR(50),
    channel VARCHAR(20),
    subject VARCHAR(500),
    sentiment VARCHAR(20),
    category VARCHAR(30),
    escalation_risk VARCHAR(20),
    confidence FLOAT,
    ai_reasoning VARCHAR(2000),
    model_used VARCHAR(100),
    classified_at TIMESTAMP_NTZ,
    loaded_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
"""

UPSERT_SQL = """
MERGE INTO {schema}.support_ticket_classifications AS target
USING (SELECT
    %(ticket_id)s AS ticket_id,
    %(created_at)s::TIMESTAMP_NTZ AS created_at,
    %(customer_id)s AS customer_id,
    %(channel)s AS channel,
    %(subject)s AS subject,
    %(sentiment)s AS sentiment,
    %(category)s AS category,
    %(escalation_risk)s AS escalation_risk,
    %(confidence)s AS confidence,
    %(ai_reasoning)s AS ai_reasoning,
    %(model_used)s AS model_used,
    %(classified_at)s::TIMESTAMP_NTZ AS classified_at
) AS source
ON target.ticket_id = source.ticket_id
WHEN MATCHED THEN UPDATE SET
    sentiment = source.sentiment,
    category = source.category,
    escalation_risk = source.escalation_risk,
    confidence = source.confidence,
    ai_reasoning = source.ai_reasoning,
    model_used = source.model_used,
    classified_at = source.classified_at,
    loaded_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    ticket_id, created_at, customer_id, channel, subject,
    sentiment, category, escalation_risk, confidence,
    ai_reasoning, model_used, classified_at
) VALUES (
    source.ticket_id, source.created_at, source.customer_id,
    source.channel, source.subject, source.sentiment,
    source.category, source.escalation_risk, source.confidence,
    source.ai_reasoning, source.model_used, source.classified_at
);
"""


class SnowflakeLoader:
    """Load classified tickets into Snowflake with idempotent MERGE.

    Args:
        account: Snowflake account identifier
        user: Snowflake username
        password: Snowflake password
        database: Target database
        schema: Target schema
        warehouse: Compute warehouse
    """

    def __init__(
        self,
        account: str,
        user: str,
        password: str,
        database: str,
        schema: str = "RAW",
        warehouse: str = "COMPUTE_WH",
    ):
        self.conn_params = {
            "account": account,
            "user": user,
            "password": password,
            "database": database,
            "schema": schema,
            "warehouse": warehouse,
        }
        self.schema = schema

    def initialize_table(self):
        """Create the target table if it doesn't exist."""
        with snowflake.connector.connect(**self.conn_params) as conn:
            conn.cursor().execute(CREATE_TABLE_SQL.format(schema=self.schema))
            logger.info("Table support_ticket_classifications initialized.")

    def load_classifications(self, records: List[dict]) -> dict:
        """Load classified tickets using MERGE for idempotency.

        Args:
            records: List of classified ticket dicts

        Returns:
            Summary dict with loaded/failed counts
        """
        if not records:
            logger.warning("No records to load.")
            return {"loaded": 0, "failed": 0}

        loaded = 0
        failed = 0

        with snowflake.connector.connect(**self.conn_params) as conn:
            cursor = conn.cursor()
            upsert_sql = UPSERT_SQL.format(schema=self.schema)

            for record in records:
                try:
                    cursor.execute(upsert_sql, record)
                    loaded += 1
                except Exception as e:
                    failed += 1
                    logger.error(
                        f"Failed to load ticket {record.get('ticket_id', '?')}: {e}"
                    )

        logger.info(f"Snowflake load complete: {loaded} loaded, {failed} failed")
        return {"loaded": loaded, "failed": failed}
