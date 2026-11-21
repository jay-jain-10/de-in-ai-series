"""
Quarantine store: the audit log of every Layer 1 / Layer 2 decision.

Idempotent by construction: UNIQUE (record_id, check_id) plus
INSERT ... ON CONFLICT DO NOTHING means an Airflow retry never double-quarantines.

Production uses RDS PostgreSQL (sql/quarantine_record.sql). This implementation
uses sqlite3 (identical ON CONFLICT semantics) so it runs and tests locally.
"""

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DECISIONS = ("fix_upstream", "accept_as_is", "confirm_error")

_NAMESPACE = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f")

SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine_record (
    quarantine_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    flagged_by_layer TEXT NOT NULL CHECK (flagged_by_layer IN ('L1','L2')),
    check_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    anomaly_type TEXT,
    confidence REAL,
    severity TEXT,
    rules_version TEXT,
    run_id TEXT,
    flagged_timestamp TEXT NOT NULL,
    reviewer_decision TEXT CHECK (reviewer_decision IN ('fix_upstream','accept_as_is','confirm_error')),
    reviewed_timestamp TEXT,
    reviewed_by TEXT,
    notes TEXT,
    record_payload TEXT,
    UNIQUE (record_id, check_id)
);
"""


def make_quarantine_id(record_id: str, check_id: str) -> str:
    """Deterministic UUID: same (record, check) always maps to the same id."""
    return str(uuid.uuid5(_NAMESPACE, f"{record_id}|{check_id}"))


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


@dataclass
class QuarantineRecord:
    record_id: str
    source_system: str
    flagged_by_layer: str            # 'L1' | 'L2'
    check_id: str
    check_name: str
    anomaly_type: Optional[str] = None
    confidence: Optional[float] = None
    severity: Optional[str] = None
    rules_version: Optional[str] = None
    run_id: Optional[str] = None
    flagged_timestamp: Optional[str] = None
    record_payload: Optional[dict] = None

    @property
    def quarantine_id(self) -> str:
        return make_quarantine_id(self.record_id, self.check_id)


class QuarantineStore:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def exists(self, record_id: str, check_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM quarantine_record WHERE record_id=? AND check_id=?",
            (record_id, check_id),
        )
        return cur.fetchone() is not None

    def is_flagged(self, record_id: str, layer: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM quarantine_record WHERE record_id=? AND flagged_by_layer=?",
            (record_id, layer),
        )
        return cur.fetchone() is not None

    def add(self, rec: QuarantineRecord) -> bool:
        """Insert if (record_id, check_id) is new. Returns True if inserted."""
        cur = self.conn.execute(
            """INSERT INTO quarantine_record
               (quarantine_id, record_id, source_system, flagged_by_layer, check_id,
                check_name, anomaly_type, confidence, severity, rules_version, run_id,
                flagged_timestamp, record_payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(record_id, check_id) DO NOTHING""",
            (
                rec.quarantine_id, rec.record_id, rec.source_system, rec.flagged_by_layer,
                rec.check_id, rec.check_name, rec.anomaly_type, rec.confidence,
                rec.severity, rec.rules_version, rec.run_id,
                rec.flagged_timestamp or utcnow_iso(),
                json.dumps(rec.record_payload, default=str) if rec.record_payload else None,
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def add_many(self, recs: List[QuarantineRecord]) -> Dict[str, int]:
        inserted = sum(1 for r in recs if self.add(r))
        return {"inserted": inserted, "skipped_duplicates": len(recs) - inserted}

    def get(self, record_id: str, check_id: str) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT * FROM quarantine_record WHERE record_id=? AND check_id=?",
            (record_id, check_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def all(self) -> List[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM quarantine_record ORDER BY flagged_timestamp, record_id, check_id")]

    def count(self, layer: Optional[str] = None) -> int:
        if layer:
            cur = self.conn.execute("SELECT COUNT(*) FROM quarantine_record WHERE flagged_by_layer=?", (layer,))
        else:
            cur = self.conn.execute("SELECT COUNT(*) FROM quarantine_record")
        return cur.fetchone()[0]

    def record_decision(
        self, record_id: str, check_id: str, decision: str, reviewed_by: str,
        notes: str = "", reviewed_timestamp: Optional[str] = None,
    ) -> bool:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
        cur = self.conn.execute(
            """UPDATE quarantine_record
               SET reviewer_decision=?, reviewed_by=?, notes=?, reviewed_timestamp=?
               WHERE record_id=? AND check_id=?""",
            (decision, reviewed_by, notes, reviewed_timestamp or utcnow_iso(), record_id, check_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def close(self):
        self.conn.close()
