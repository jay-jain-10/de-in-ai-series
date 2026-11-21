"""Shared fixtures: fake Anthropic client (no network), fake clocks, wired gateway."""

import json
from types import SimpleNamespace

import pytest

from src.cache import InMemoryCache, ReviewCache
from src.circuit_breaker import BreakerRegistry
from src.config import load_router_config
from src.gateway import ModelGateway
from src.patterns import Orchestrator
from src.queues import InMemoryQueue
from src.router import ModelRouter


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _default_payload(task, text):
    if task == "sentiment":
        neg = any(w in text.lower() for w in ("terrible", "buggy", "broke", "awful", "refused"))
        return {"sentiment": "negative" if neg else "positive", "confidence": 0.92, "reasoning": "fake"}
    if task == "aspect_extraction":
        return {"aspects": ["battery"], "unsafe_language": "idiot" in text.lower(),
                "confidence": 0.9, "reasoning": "fake"}
    return {"toxic": "idiot" in text.lower(), "categories": [], "confidence": 0.95, "reasoning": "fake"}


class FakeAnthropic:
    """Mimics anthropic.Anthropic().messages.create. `handler(model, task, text)` may return
    a dict payload, a raw string, or raise. Token usage is fixed (120 in / 15 out)."""

    def __init__(self, handler=None, latency=None):
        self.handler = handler
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.latency = latency

    @staticmethod
    def task_of(prompt):
        if "sentiment classifier" in prompt:
            return "sentiment"
        if "extract product aspects" in prompt:
            return "aspect_extraction"
        return "toxicity_detection"

    def _create(self, model, max_tokens, messages, timeout=None):
        prompt = messages[0]["content"]
        task = self.task_of(prompt)
        text = prompt.rsplit("Review: ", 1)[-1]
        self.calls.append({"model": model, "task": task, "text": text, "timeout": timeout,
                           "prompt": prompt})
        out = self.handler(model, task, text) if self.handler else None
        if out is None:
            out = _default_payload(task, text)
        raw = out if isinstance(out, str) else json.dumps(out)
        return SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            usage=SimpleNamespace(input_tokens=120, output_tokens=15),
        )

    def calls_for(self, model_substr=None, task=None):
        return [c for c in self.calls
                if (model_substr is None or model_substr in c["model"])
                and (task is None or c["task"] == task)]


@pytest.fixture
def config():
    return load_router_config()


@pytest.fixture
def sleeps():
    return []


def make_stack(client, config, cache=True, clock=None, sleeps=None, rng=lambda: 0.0):
    clock = clock or FakeClock()
    breakers = BreakerRegistry(config.breaker, clock=clock, rng=rng)
    rc = ReviewCache(InMemoryCache(clock), config.cache_ttl_seconds) if cache else None
    gw = ModelGateway(client, config, cache=rc, breakers=breakers,
                      retry_queue=InMemoryQueue("retry", clock), clock=clock,
                      sleep=(lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None))
    return gw, Orchestrator(gw, ModelRouter(config), clock=clock), clock


@pytest.fixture
def stack(config):
    client = FakeAnthropic()
    gw, orch, clock = make_stack(client, config)
    return SimpleNamespace(client=client, gateway=gw, orch=orch, clock=clock)


@pytest.fixture
def review():
    return {"review_id": "rev_1", "text": "The battery life is good but the software is buggy and support refused to help."}
