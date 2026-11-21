"""Idempotent enrichment persistence keyed on (event_id, enrichment_version).

InMemoryEnrichmentStore is the test fake. PostgresEnrichmentStore issues a
real UPSERT through any DB-API connection (psycopg2 in prod, sqlite3 in tests).
"""

from typing import Optional
from src.models import EnrichmentRecord

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS enrichments (
    event_id TEXT NOT NULL,
    enrichment_version INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    anomaly_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT,
    stale_context INTEGER NOT NULL DEFAULT 0,
    enriched_at REAL NOT NULL,
    PRIMARY KEY (event_id, enrichment_version)
)
"""

UPSERT_SQL = """
INSERT INTO enrichments
    (event_id, enrichment_version, user_id, risk_score, anomaly_type,
     confidence, reasoning, stale_context, enriched_at)
VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
ON CONFLICT (event_id, enrichment_version) DO UPDATE SET
    risk_score = excluded.risk_score,
    anomaly_type = excluded.anomaly_type,
    confidence = excluded.confidence,
    reasoning = excluded.reasoning,
    stale_context = excluded.stale_context,
    enriched_at = excluded.enriched_at
"""


class InMemoryEnrichmentStore:
    def __init__(self):
        self.rows = {}
        self.upserts = 0

    def exists(self, event_id: str, version: int) -> bool:
        return (event_id, version) in self.rows

    def upsert(self, rec: EnrichmentRecord) -> None:
        self.upserts += 1
        self.rows[(rec.event_id, rec.enrichment_version)] = rec

    def latest(self, event_id: str) -> Optional[EnrichmentRecord]:
        versions = [v for (e, v) in self.rows if e == event_id]
        return self.rows[(event_id, max(versions))] if versions else None

    def count(self) -> int:
        return len(self.rows)


class PostgresEnrichmentStore:
    """paramstyle: '%s' for psycopg2, '?' for sqlite3."""

    def __init__(self, conn, paramstyle: str = "%s"):
        self.conn = conn
        self.p = paramstyle

    def create_schema(self):
        cur = self.conn.cursor()
        cur.execute(SCHEMA_SQL)
        self.conn.commit()

    def exists(self, event_id: str, version: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT 1 FROM enrichments WHERE event_id = {self.p} AND enrichment_version = {self.p}",
            (event_id, version),
        )
        return cur.fetchone() is not None

    def upsert(self, rec: EnrichmentRecord) -> None:
        cur = self.conn.cursor()
        cur.execute(
            UPSERT_SQL.format(p=self.p),
            (rec.event_id, rec.enrichment_version, rec.user_id, rec.risk_score,
             rec.anomaly_type, rec.confidence, rec.reasoning, int(rec.stale_context), rec.enriched_at),
        )
        self.conn.commit()

    def latest(self, event_id: str) -> Optional[EnrichmentRecord]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT event_id, enrichment_version, user_id, risk_score, anomaly_type, confidence, "
            f"reasoning, stale_context, enriched_at FROM enrichments WHERE event_id = {self.p} "
            "ORDER BY enrichment_version DESC LIMIT 1",
            (event_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return EnrichmentRecord(event_id=r[0], enrichment_version=r[1], user_id=r[2], risk_score=r[3],
                                anomaly_type=r[4], confidence=r[5], reasoning=r[6] or "",
                                stale_context=bool(r[7]), enriched_at=r[8])

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM enrichments")
        return cur.fetchone()[0]
