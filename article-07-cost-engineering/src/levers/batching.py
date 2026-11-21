"""Lever 4: Batching (50% discount via the Anthropic Message Batches API).

Accumulate 100-1000 requests, submit, poll, then record per-request cost events
at the batch discount. Only for latency-tolerant workloads (nightly ETL,
daily claims, moderation) - NOT fraud detection or chatbots.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from src.cost_events import CostEvent
from src.pricing import compute_cost_usd

logger = logging.getLogger(__name__)

LATENCY_SENSITIVE_TASKS = {"fraud_detection", "chatbot", "customer_support_chat"}


def is_batchable(task_type: str) -> bool:
    return task_type not in LATENCY_SENSITIVE_TASKS


@dataclass
class BatchItem:
    record_id: str
    prompt: str
    model: str
    max_tokens: int = 300


class BatchAccumulator:
    def __init__(self, min_size: int = 100, max_size: int = 1000):
        self.min_size, self.max_size = min_size, max_size
        self.items: List[BatchItem] = []

    def add(self, item: BatchItem) -> bool:
        """Add an item; True when the batch is full and should be flushed."""
        self.items.append(item)
        return len(self.items) >= self.max_size

    def ready(self) -> bool:
        return len(self.items) >= self.min_size

    def drain(self) -> List[BatchItem]:
        items, self.items = self.items, []
        return items


class BatchSubmitter:
    def __init__(self, client, tracker=None, pipeline_name: str = "batch",
                 team_name: str = "unknown", task_type: str = "classification"):
        self.client = client
        self.tracker = tracker
        self.pipeline_name, self.team_name, self.task_type = pipeline_name, team_name, task_type

    def submit(self, items: List[BatchItem]) -> str:
        for it in items:
            # API requirement: custom_id must match ^[a-zA-Z0-9_-]{1,64}$
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", it.record_id):
                raise ValueError(f"record_id {it.record_id!r} is not a valid batch custom_id")
        requests = [{
            "custom_id": it.record_id,
            "params": {"model": it.model, "max_tokens": it.max_tokens,
                       "messages": [{"role": "user", "content": it.prompt}]},
        } for it in items]
        batch = self.client.messages.batches.create(requests=requests)
        self._models = {it.record_id: it.model for it in items}
        return batch.id

    def wait_until_ended(self, batch_id: str, poll_seconds: float = 30.0,
                         timeout_seconds: float = 24 * 3600, sleep=time.sleep) -> None:
        """Poll until processing_status == 'ended' (batches expire after 24h)."""
        waited = 0.0
        while True:
            status = self.client.messages.batches.retrieve(batch_id).processing_status
            if status == "ended":
                return
            if waited >= timeout_seconds:
                raise TimeoutError(f"batch {batch_id} still {status!r} after {timeout_seconds}s")
            sleep(poll_seconds)
            waited += poll_seconds

    def collect(self, batch_id: str, models: Optional[Dict[str, str]] = None,
                wait: bool = True, **wait_kwargs) -> Dict[str, str]:
        """Wait for the batch to end, record cost events at 50% discount.

        Returns {record_id: text}. Results arrive in arbitrary order (matched by
        custom_id). Errored/canceled/expired items are not billed and are skipped.
        """
        if wait:
            self.wait_until_ended(batch_id, **wait_kwargs)
        models = models or getattr(self, "_models", {})
        out: Dict[str, str] = {}
        for res in self.client.messages.batches.results(batch_id):
            rid = res.custom_id
            if res.result.type != "succeeded":
                logger.warning("Batch item %s %s", rid, res.result.type)
                continue
            msg = res.result.message
            model = models.get(rid, msg.model)
            ev = CostEvent(
                pipeline_name=self.pipeline_name, team_name=self.team_name,
                task_type=self.task_type, record_id=rid, model=model,
                input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
                cost_usd=compute_cost_usd(model, msg.usage.input_tokens,
                                          msg.usage.output_tokens, batch=True),
                batch=True,
            )
            if self.tracker:
                self.tracker.record(ev)
            # Newer models can return non-text (e.g. thinking) blocks first.
            out[rid] = next((b.text for b in msg.content if getattr(b, "type", "text") == "text"), "")
        return out
