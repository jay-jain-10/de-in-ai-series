"""Thin AWS Lambda handlers used by the Step Functions state machine.

Events carry {bucket, key, contract_id}; heavy objects (boto3 clients) are
created lazily and can be injected in tests via the `deps` argument.
"""

import json
from typing import Dict, Optional

from src.config import PipelineConfig
from src.dlq import DeadLetterQueue, build_error_record, summarize_quarantine
from src.format_detector import detect_format


def _s3():
    import boto3
    return boto3.client("s3")


def detect_format_handler(event: Dict, context=None, deps: Optional[Dict] = None) -> Dict:
    """State: DetectFormat. Returns metadata + doc_type for the Choice state."""
    deps = deps or {}
    config = deps.get("config") or PipelineConfig()
    s3 = deps.get("s3") or _s3()
    body = s3.get_object(Bucket=event["bucket"], Key=event["key"])["Body"].read()
    meta = detect_format(event["contract_id"], body, config)
    return {**event, "metadata": meta.model_dump(), "doc_type": meta.doc_type,
            "total_pages": meta.total_pages,
            "use_fargate": meta.total_pages > config.fargate_page_threshold}


def dead_letter_handler(event: Dict, context=None, deps: Optional[Dict] = None) -> Dict:
    """State: SendToDLQ (Catch target). Event carries Step Functions error info."""
    deps = deps or {}
    config = deps.get("config") or PipelineConfig()
    dlq = DeadLetterQueue(deps.get("s3") or _s3(), config)
    err = event.get("error", {}) or {}
    cause = err.get("Cause", "unknown")
    if isinstance(cause, str):
        try:
            cause = json.loads(cause).get("errorMessage", cause)
        except ValueError:
            pass
    record = build_error_record(
        event["contract_id"], event["key"], event["bucket"],
        event.get("failure_stage", err.get("Error", "unknown")), str(cause),
        attempt_count=event.get("attempt_count", 1),
        last_attempt_model=config.anthropic.extraction_model)
    dlq.send(record, event["key"])
    return {**event, "dlq_record": record}


def daily_quarantine_summary_handler(event: Dict, context=None, deps: Optional[Dict] = None) -> Dict:
    """Scheduled Lambda: summarise quarantine and emit review tickets."""
    deps = deps or {}
    dlq = DeadLetterQueue(deps.get("s3") or _s3(), deps.get("config") or PipelineConfig())
    return summarize_quarantine(dlq.list_errors())
