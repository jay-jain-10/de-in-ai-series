"""Idempotent result storage: composite key (review_id, model_version, task_type).

Re-running the pipeline MERGEs (upserts) rather than inserting, so table state converges.
In-memory store for tests/local; MERGE_SQL is the warehouse statement from the article.
"""

import re
import threading
from typing import Optional

from .schemas import TaskResult

RESULTS_TABLE = "review_results"
FALLBACK_TABLE = "review_results_fallback"
HUMAN_REVIEW_TABLE = "needs_human_review"

MERGE_SQL = """MERGE INTO {table} t
USING new_results s
ON t.review_id = s.review_id AND t.model_version = s.model_version AND t.task_type = s.task_type
WHEN MATCHED THEN UPDATE SET t.confidence = s.confidence, t.result = s.result
WHEN NOT MATCHED THEN INSERT (review_id, model_version, task_type, result, confidence)
  VALUES (s.review_id, s.model_version, s.task_type, s.result, s.confidence)"""


def idempotency_key(review_id: str, model_version: str, task_type: str) -> tuple:
    return (review_id, model_version, task_type)


def row_key(row: dict) -> tuple:
    return idempotency_key(row["review_id"], row["model_version"], row["task_type"])


def to_row(r: TaskResult) -> dict:
    return {
        "review_id": r.review_id, "model_version": r.model_version, "task_type": r.task,
        "model": r.model, "result": r.result, "confidence": r.confidence,
        "fallback": r.fallback, "failure_reason": r.failure_reason,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
        "cost_usd": r.cost_usd, "latency_ms": r.latency_ms, "parent_task": r.parent_task,
    }


def route_table(row: dict, human_review_confidence: float = 0.7) -> str:
    """Routing is driven by output data: fallbacks are quarantined, and low-confidence
    toxicity results go to needs_human_review."""
    if row["fallback"]:
        return FALLBACK_TABLE
    if row["task_type"] == "toxicity_detection" and row["confidence"] < human_review_confidence:
        return HUMAN_REVIEW_TABLE
    return RESULTS_TABLE


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


def merge_sql(table: str = RESULTS_TABLE) -> str:
    # The table name is spliced into SQL text (identifiers cannot be bound parameters),
    # so only accept plain [db.][schema.]table identifiers.
    if not _IDENTIFIER.match(table):
        raise ValueError(f"invalid table identifier: {table!r}")
    return MERGE_SQL.format(table=table)


class InMemoryResultStore:
    TABLES = (RESULTS_TABLE, FALLBACK_TABLE, HUMAN_REVIEW_TABLE)

    def __init__(self, human_review_confidence: float = 0.7):
        self.tables = {t: {} for t in self.TABLES}
        self.human_review_confidence = human_review_confidence
        self._lock = threading.Lock()

    def merge(self, rows: list, table: Optional[str] = None) -> dict:
        """Upsert rows by composite key. A key lives in exactly one table at a time:
        if a re-run turns a fallback into a success, the stale fallback row is removed."""
        inserted = updated = 0
        with self._lock:
            for row in rows:
                target = table or route_table(row, self.human_review_confidence)
                key = row_key(row)
                for name, data in self.tables.items():
                    if name != target:
                        data.pop(key, None)
                if key in self.tables[target]:
                    updated += 1
                else:
                    inserted += 1
                self.tables[target][key] = row
        return {"inserted": inserted, "updated": updated}

    def rows(self, table: str) -> list:
        with self._lock:
            return list(self.tables[table].values())

    def count(self, table: str) -> int:
        with self._lock:
            return len(self.tables[table])

    def fallback_rate(self) -> float:
        fb = self.count(FALLBACK_TABLE)
        total = sum(self.count(t) for t in self.TABLES)
        return fb / total if total else 0.0
