"""Cost-tracking wrapper around Claude calls.

Every AI call goes through CostTrackingClient.complete(), which:
  1. checks the pipeline's daily budget (throttle/pause),
  2. calls Claude (injected client -> mockable),
  3. computes cost_tokens / cost_usd,
  4. emits a CostEvent to the sink (cost_events table) and metrics emitter,
  5. flags per-record SLA violations (retries included).
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from src.cost_events import CostEvent, CostSink, InMemorySink
from src.cost_sla import SLAEnforcer
from src.pricing import compute_cost_usd

logger = logging.getLogger(__name__)


@dataclass
class CallResult:
    text: str
    event: CostEvent

    @property
    def cost_usd(self) -> float:
        return self.event.cost_usd

    @property
    def cost_tokens(self) -> int:
        return self.event.cost_tokens


class CostTrackingClient:
    def __init__(
        self,
        client=None,
        sink: CostSink = None,
        enforcer: SLAEnforcer = None,
        metrics=None,
        api_key: str = "",
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client
        self.sink = sink if sink is not None else InMemorySink()
        self.enforcer = enforcer or SLAEnforcer()
        self.metrics = metrics
        self._clock = clock
        self._now = now

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        pipeline_name: str,
        team_name: str = "unknown",
        task_type: str = "classification",
        record_id: Optional[str] = None,
        max_tokens: int = 300,
        system: Optional[str] = None,
        attempt: int = 1,
        validator: Optional[Callable[[str], bool]] = None,
    ) -> CallResult:
        now = self._now().isoformat()
        self.enforcer.check_before_call(pipeline_name, now[:10])

        kwargs = dict(model=model, max_tokens=max_tokens,
                      messages=[{"role": "user", "content": prompt}])
        if system:
            kwargs["system"] = system

        start = self._clock()
        response = self.client.messages.create(**kwargs)
        latency_ms = int((self._clock() - start) * 1000)

        text = response.content[0].text if response.content else ""
        ok = validator(text) if validator else True
        event = CostEvent(
            pipeline_name=pipeline_name, team_name=team_name, task_type=task_type,
            record_id=record_id, model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=compute_cost_usd(model, response.usage.input_tokens,
                                      response.usage.output_tokens),
            attempt=attempt, is_retry=attempt > 1, success=ok,
            latency_ms=latency_ms, created_at=now,
        )
        self.record(event)
        return CallResult(text=text, event=event)

    def record(self, event: CostEvent) -> None:
        """Observe + persist an event (also used for cache hits and batch results).

        The spend is observed by the budget enforcer first: the API call has
        already been paid for, so a sink outage must not hide it from the
        daily-budget throttle."""
        self.enforcer.observe(event)
        if self.metrics is not None:
            self.metrics.observe(event)
        self.sink.write([event])
