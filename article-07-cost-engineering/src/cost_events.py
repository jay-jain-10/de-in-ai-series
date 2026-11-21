"""cost_events: the cost lineage record and the sinks that persist it.

One CostEvent per API call (including retries and cache hits). The event
captures everything the article lists: timestamp, model, input/output tokens,
cache_hit, pipeline_name, team_name, task_type, cost_usd, latency_ms, plus
attempt/is_retry so retries do not silently inflate unit costs.
"""

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class CostEvent:
    pipeline_name: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    team_name: str = "unknown"
    task_type: str = "classification"
    record_id: Optional[str] = None
    cache_hit: bool = False
    batch: bool = False
    attempt: int = 1
    is_retry: bool = False
    success: bool = True
    latency_ms: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def cost_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def created_date(self) -> str:
        return self.created_at[:10]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cost_tokens"] = self.cost_tokens
        d["created_date"] = self.created_date
        return d


def annotate_row(row: dict, events: List[CostEvent]) -> dict:
    """Cost as metadata: stamp cost_tokens / cost_usd onto an AI-produced row.

    Sums all events for the record (retries included) so the row carries the
    true cost of producing it.
    """
    out = dict(row)
    out["cost_tokens"] = sum(e.cost_tokens for e in events)
    out["cost_usd"] = round(sum(e.cost_usd for e in events), 8)
    out["cost_attempts"] = len(events)
    out["cost_cache_hit"] = bool(events) and all(e.cache_hit for e in events)
    return out


class CostSink(Protocol):
    def write(self, events: List[CostEvent]) -> None: ...


class InMemorySink:
    def __init__(self):
        self.events: List[CostEvent] = []

    def write(self, events: List[CostEvent]) -> None:
        self.events.extend(events)


class FirehoseSink:
    """Lambda -> Kinesis Data Firehose -> S3 / Snowflake. boto3 client is injected."""

    def __init__(self, stream_name: str, client=None, region: str = "us-east-1",
                 max_retries: int = 2):
        if client is None:
            import boto3
            client = boto3.client("firehose", region_name=region)
        self.client = client
        self.stream_name = stream_name
        self.max_retries = max_retries

    def write(self, events: List[CostEvent]) -> None:
        for i in range(0, len(events), 500):  # PutRecordBatch limit
            records = [{"Data": (json.dumps(e.to_dict()) + "\n").encode()}
                       for e in events[i:i + 500]]
            # PutRecordBatch does NOT raise on partial failure: it returns
            # FailedPutCount and a per-record ErrorCode. Retry only the failed
            # records; if some still fail, raise rather than silently losing
            # cost lineage rows.
            for _ in range(self.max_retries + 1):
                resp = self.client.put_record_batch(
                    DeliveryStreamName=self.stream_name, Records=records)
                failed = _failed_firehose_records(resp, records)
                if not failed:
                    break
                records = failed
            else:
                raise RuntimeError(
                    f"Firehose {self.stream_name}: {len(records)} cost events failed after "
                    f"{self.max_retries + 1} attempts")


def _failed_firehose_records(resp, records: List[dict]) -> List[dict]:
    if not isinstance(resp, dict) or not resp.get("FailedPutCount"):
        return []
    results = resp.get("RequestResponses") or []
    return [rec for rec, r in zip(records, results) if r.get("ErrorCode")]


COST_EVENTS_INSERT_SQL = """
INSERT INTO {schema}.cost_events (
    created_at, created_date, pipeline_name, team_name, task_type, record_id,
    model, input_tokens, output_tokens, cost_tokens, cost_usd,
    cache_hit, batch, attempt, is_retry, success, latency_ms
) VALUES (
    %(created_at)s, %(created_date)s, %(pipeline_name)s, %(team_name)s, %(task_type)s,
    %(record_id)s, %(model)s, %(input_tokens)s, %(output_tokens)s, %(cost_tokens)s,
    %(cost_usd)s, %(cache_hit)s, %(batch)s, %(attempt)s, %(is_retry)s, %(success)s,
    %(latency_ms)s
)
"""


class SnowflakeCostSink:
    """Writes cost events to Snowflake. Connection is injected (no live deps)."""

    def __init__(self, connection, schema: str = "RAW"):
        self.conn = connection
        self.schema = schema

    def write(self, events: List[CostEvent]) -> None:
        if not events:
            return
        cur = self.conn.cursor()
        try:
            cur.executemany(
                COST_EVENTS_INSERT_SQL.format(schema=self.schema),
                [e.to_dict() for e in events],
            )
            self.conn.commit()
        finally:
            cur.close()
