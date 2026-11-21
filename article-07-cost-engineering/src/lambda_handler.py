"""Lambda cost wrapper: every AI call goes through this thin layer.

Event: {"pipeline_name":..., "team_name":..., "task_type":...,
        "records": [{"record_id":..., "text":...}, ...]}
Returns rows annotated with cost_tokens / cost_usd. Metrics are flushed once
per invocation. Pipeline construction is injectable for tests.
"""

import json
import logging
from typing import Callable, Optional

from src.config import FinOpsConfig
from src.cost_events import FirehoseSink
from src.cost_sla import CostBudgetExceeded, SLAEnforcer
from src.cost_tracker import CostTrackingClient
from src.metrics import MetricEmitter
from src.pipeline import CostAwarePipeline

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_pipeline_factory: Optional[Callable[[dict], tuple]] = None
_enforcer = None


def set_pipeline_factory(factory: Optional[Callable[[dict], tuple]]) -> None:
    """factory(event) -> (CostAwarePipeline, MetricEmitter)."""
    global _pipeline_factory
    _pipeline_factory = factory


def _default_factory(event: dict):
    global _enforcer
    cfg = FinOpsConfig()
    _enforcer = _enforcer or SLAEnforcer(cfg.sla)  # persists across warm invocations
    metrics = MetricEmitter(namespace=cfg.aws.cloudwatch_namespace, region=cfg.aws.region)
    tracker = CostTrackingClient(
        api_key=cfg.anthropic.api_key,
        sink=FirehoseSink(cfg.aws.firehose_stream, region=cfg.aws.region),
        enforcer=_enforcer, metrics=metrics)
    pipe = CostAwarePipeline(
        tracker, pipeline_name=event.get("pipeline_name", "default"),
        team_name=event.get("team_name", "unknown"),
        task_type=event.get("task_type", "classification"))
    return pipe, metrics


def handler(event, context=None):
    try:
        pipe, metrics = (_pipeline_factory or _default_factory)(event)
    except Exception as e:
        logger.exception("Failed to build pipeline")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

    records = event.get("records") or []
    if not records:
        return {"statusCode": 400, "body": json.dumps({"error": "no records"})}

    rows, status = [], 200
    for rec in records:
        try:
            rows.append(pipe.process(rec["record_id"], rec["text"]))
        except CostBudgetExceeded as e:
            logger.warning("Throttled: %s", e)
            status = 429
            rows.append({"record_id": rec["record_id"], "error": "budget_exceeded"})
            break  # pause the pipeline for the rest of the batch
    metrics.flush()

    body = {"rows": rows,
            "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 8),
            "total_cost_tokens": sum(r.get("cost_tokens", 0) for r in rows)}
    return {"statusCode": status, "body": json.dumps(body)}
