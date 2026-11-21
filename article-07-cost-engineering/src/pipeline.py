"""Cost-aware pipeline: cache -> tier -> optimized prompt -> tracked call -> fallback.

Batching (lever 4) is the offline path: see levers/batching.py.
"""

import json
import logging
from typing import Callable, Dict, Optional

from src.cost_events import CostEvent, annotate_row
from src.cost_tracker import CostTrackingClient
from src.levers.caching import ResponseCache
from src.levers.model_tiering import ModelRouter
from src.levers.prompt_optimization import build_optimized_prompt

logger = logging.getLogger(__name__)


def valid_json(text: str) -> bool:
    try:
        obj = json.loads(text)
        return isinstance(obj, dict)
    except (ValueError, TypeError):
        return False


class CostAwarePipeline:
    def __init__(self, tracker: CostTrackingClient, cache: ResponseCache = None,
                 router: ModelRouter = None, pipeline_name: str = "ticket_classification",
                 team_name: str = "support-data", task_type: str = "classification",
                 validator: Callable[[str], bool] = valid_json):
        self.tracker = tracker
        self.cache = cache or ResponseCache()
        self.router = router or ModelRouter()
        self.pipeline_name, self.team_name, self.task_type = pipeline_name, team_name, task_type
        self.validator = validator

    def process(self, record_id: str, text: str) -> Dict:
        events = []
        cached = self.cache.get(text)
        if cached:
            response, kind = cached
            ev = CostEvent(pipeline_name=self.pipeline_name, team_name=self.team_name,
                           task_type=self.task_type, record_id=record_id, model="cache",
                           input_tokens=0, output_tokens=0, cost_usd=0.0, cache_hit=True)
            self.tracker.record(ev)
            return annotate_row({"record_id": record_id, "result": response,
                                 "model_used": f"cache:{kind}"}, [ev])

        prompt = build_optimized_prompt(text)
        route = self.router.route(text)
        attempt = 1
        while True:
            res = self.tracker.complete(
                prompt["user"], system=prompt["system"], model=route.model,
                pipeline_name=self.pipeline_name, team_name=self.team_name,
                task_type=self.task_type, record_id=record_id, attempt=attempt,
                validator=self.validator)
            events.append(res.event)
            if res.event.success:
                break
            nxt = self.router.fallback(route)
            if nxt is None:
                break
            route, attempt = nxt, attempt + 1

        ok = events[-1].success
        if ok:
            self.cache.put(text, res.text)
        return annotate_row({"record_id": record_id, "result": res.text if ok else None,
                             "model_used": route.model, "success": ok}, events)
