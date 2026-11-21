"""The three SLAs from the article, as pure functions over timestamps."""

import statistics
from datetime import datetime, timedelta
from typing import Dict, List

EXTRACTION_SLA_PERCENTILE = 0.95
EXTRACTION_SLA = timedelta(hours=4)       # P95 upload -> Snowflake MERGE
HUMAN_REVIEW_SLA = timedelta(hours=24)    # quarantine -> review complete
FEEDBACK_LOOP_SLA = timedelta(days=7)     # correction -> training data


def percentile(values: List[float], q: float) -> float:
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(-(-q * len(s) // 1)) - 1))  # nearest-rank
    return s[idx]


def check_extraction_sla(latencies: List[timedelta]) -> Dict:
    """95% of contracts processed within 4 hours (P95 latency)."""
    p95 = timedelta(seconds=percentile([l.total_seconds() for l in latencies], EXTRACTION_SLA_PERCENTILE))
    return {"p95": p95, "breached": p95 > EXTRACTION_SLA, "action": "alert ops team" if p95 > EXTRACTION_SLA else None}


def _now_like(ts: datetime) -> datetime:
    """Current time with the same tz-awareness as `ts` (avoids naive/aware TypeError)."""
    return datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()


def check_review_sla(quarantined_at: datetime, reviewed_at: datetime = None, now: datetime = None) -> Dict:
    end = reviewed_at or now or _now_like(quarantined_at)
    elapsed = end - quarantined_at
    breached = elapsed > HUMAN_REVIEW_SLA
    return {"elapsed": elapsed, "breached": breached,
            "action": "escalate to VP of Product" if breached else None}


def check_feedback_sla(corrected_at: datetime, ingested_at: datetime = None, now: datetime = None) -> Dict:
    end = ingested_at or now or _now_like(corrected_at)
    lag = end - corrected_at
    breached = lag > FEEDBACK_LOOP_SLA
    return {"lag": lag, "breached": breached,
            "action": "increase review prioritization / prompt refinement cadence" if breached else None}


def review_resolution_rate(quarantine_events: List[Dict]) -> float:
    """Share of quarantined docs resolved within 24h (event: quarantined_at, resolved_at|None)."""
    if not quarantine_events:
        return 1.0
    ok = sum(1 for e in quarantine_events
             if e.get("resolved_at") and e["resolved_at"] - e["quarantined_at"] <= HUMAN_REVIEW_SLA)
    return ok / len(quarantine_events)
