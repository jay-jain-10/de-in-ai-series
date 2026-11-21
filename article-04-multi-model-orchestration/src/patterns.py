"""The four orchestration patterns: Router, Chain, Fan-Out/Fan-In, Fallback Cascade."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import RouterConfig
from .gateway import ModelGateway
from .latency import LatencyRecorder
from .queues import InMemoryQueue
from .router import ModelRouter
from .schemas import TASKS, ContractViolation, TaskResult, validate_task_output


@dataclass
class PatternResult:
    review_id: str
    pattern: str
    results: dict = field(default_factory=dict)  # task -> TaskResult
    latency_ms: float = 0.0
    needs_human_review: bool = False
    # Paid calls whose output was not kept (e.g. cascade steps that were escalated past).
    discarded: list = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.results.values()) + sum(
            r.cost_usd for r in self.discarded)

    @property
    def models_called(self) -> list:
        return [r.model for r in self.results.values() if not r.cache_hit and not r.fallback]

    def task_results(self) -> list:
        return list(self.results.values())


class Orchestrator:
    def __init__(
        self,
        gateway: ModelGateway,
        router: ModelRouter,
        recorder: Optional[LatencyRecorder] = None,
        human_review_queue: Optional[InMemoryQueue] = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.gateway = gateway
        self.router = router
        self.config: RouterConfig = gateway.config
        self.recorder = recorder if recorder is not None else LatencyRecorder()
        self.human_queue = human_review_queue if human_review_queue is not None else InMemoryQueue("human_review")
        self._clock = clock

    def run(self, pattern: str, review: dict) -> PatternResult:
        fn = {"router": self.run_router, "chain": self.run_chain,
              "fan_out": self.run_fan_out, "cascade": self.run_cascade}[pattern]
        return fn(review)

    def _timed(self, pattern: str, review: dict, body: Callable) -> PatternResult:
        started = self._clock()
        out = PatternResult(review["review_id"], pattern)
        body(out)
        out.latency_ms = round((self._clock() - started) * 1000, 3)
        self.recorder.record(pattern, out.latency_ms)
        return out

    # ---- Pattern 1: Router -------------------------------------------
    def run_router(self, review: dict) -> PatternResult:
        """Classify simple/complex, then run ALL three tasks on one model."""
        cfg = self.config.patterns["router"]

        def body(out):
            kind = self.router.classify_complexity(review["text"])
            alias = cfg["simple_model"] if kind == "simple" else cfg["complex_model"]
            why = f"router_pattern={kind}, words={len(review['text'].split())}, used_{alias}"
            for task in TASKS:
                out.results[task] = self.gateway.call(
                    task, alias, review["review_id"], review["text"], decision=why)
        return self._timed("router", review, body)

    # ---- Pattern 2: Chain --------------------------------------------
    def run_chain(self, review: dict) -> PatternResult:
        """sentiment (Haiku) -> if negative: aspects (Sonnet) -> if unsafe: toxicity (Opus)."""
        cfg = self.config.patterns["chain"]
        rid, text = review["review_id"], review["text"]

        def body(out):
            sent = self.gateway.call("sentiment", cfg["sentiment_model"], rid, text,
                                     decision="chain_step_1")
            out.results["sentiment"] = sent
            if sent.fallback:
                out.needs_human_review = True
                return
            try:  # inter-model contract: validate upstream output before using it as context
                validate_task_output("sentiment", sent.result)
            except ContractViolation as e:
                self.gateway.record_contract_violation()
                self.gateway.dlq.enqueue(rid, "sentiment", f"chain_contract: {e}")
                out.needs_human_review = True
                return
            if sent.result["sentiment"] != "negative":
                return  # positive/neutral: skip expensive steps
            asp = self.gateway.call(
                "aspect_extraction", cfg["aspect_model"], rid, text,
                context={"sentiment": sent.result["sentiment"], "confidence": sent.confidence},
                decision="chain_step_2: sentiment=negative", parent_task="sentiment")
            out.results["aspect_extraction"] = asp
            if asp.fallback:
                out.needs_human_review = True
                return
            if asp.result.get("unsafe_language"):
                tox = self.gateway.call(
                    "toxicity_detection", cfg["toxicity_model"], rid, text,
                    decision="chain_step_3: unsafe_language=true", parent_task="aspect_extraction")
                out.results["toxicity_detection"] = tox
                if tox.fallback or tox.confidence < self.config.human_review_confidence:
                    out.needs_human_review = True
        return self._timed("chain", review, body)

    # ---- Pattern 3: Fan-Out / Fan-In ---------------------------------
    def run_fan_out(self, review: dict) -> PatternResult:
        """All three tasks in parallel (Haiku / Sonnet / Opus); latency = max of the three."""
        cfg = self.config.patterns["fan_out"]

        def body(out):
            with ThreadPoolExecutor(max_workers=len(TASKS)) as pool:
                futures = {
                    task: pool.submit(
                        self.gateway.call, task, cfg[task], review["review_id"],
                        review["text"], None, "fan_out")
                    for task in TASKS
                }
                for task, fut in futures.items():  # fan-in / merge
                    out.results[task] = fut.result()
            out.needs_human_review = any(r.fallback for r in out.results.values())
        return self._timed("fan_out", review, body)

    # ---- Pattern 4: Fallback Cascade ---------------------------------
    def run_cascade(self, review: dict, task: Optional[str] = None) -> PatternResult:
        """Cheapest model first; escalate while confidence < threshold; else fallback + human."""
        cfg = self.config.patterns["cascade"]
        task = task or cfg["task"]
        threshold = cfg["confidence_threshold"]
        rid, text = review["review_id"], review["text"]

        def body(out):
            last_alias = cfg["models"][-1]
            for step, alias in enumerate(cfg["models"], start=1):
                res = self.gateway.call(task, alias, rid, text,
                                        decision=f"cascade_step_{step}, threshold={threshold}")
                last_alias = alias
                if not res.fallback and res.confidence >= threshold:
                    out.results[task] = res
                    return
                out.discarded.append(res)  # paid for, but escalated past
            reason = "low_confidence_after_cascade" if not res.fallback else res.failure_reason
            fb = self.gateway.make_fallback(
                rid, task, last_alias, reason, decision="cascade_exhausted")
            fb.cost_usd = 0.0
            out.results[task] = fb
            out.needs_human_review = True
            self.human_queue.enqueue(rid, task, reason, {"models_tried": list(cfg["models"])})
        return self._timed("cascade", review, body)
