"""Shared fixtures. Everything is offline: Anthropic is replaced by FakeLLM, Kafka by
InMemoryPublisher, Redis/S3 by in-memory caches (or MagicMock in adapter tests),
Snowflake by InMemoryWarehouse (or MagicMock connection in adapter tests)."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import PlatformConfig
from src.contracts import IncomingDocument, SourceType
from src.gateway.app import build_service, create_app
from src.gateway.cache import InMemoryCache, TieredCache
from src.gateway.circuit_breaker import CircuitBreaker
from src.gateway.client import GatewayClient
from src.gateway.llm import LLMResult
from src.gateway.quota import QuotaManager, TeamAccount
from src.messaging import InMemoryPublisher
from src.observability import MetricsRegistry
from src.pipeline import Platform

ROOT = Path(__file__).resolve().parent.parent
CLAIMS_YAML = (ROOT / "pipelines" / "claims-processing.yaml").read_text()
COMPLIANCE_YAML = (ROOT / "pipelines" / "compliance-contracts.yaml").read_text()

KEYS = {"claims": "key-claims", "compliance": "key-compliance", "data": "key-data"}


def claim_text(name="Jane Doe", policy="POL-123456", incident="2026-10-01", amount="4,500.00",
               procedure="99213", diagnosis="J20.9") -> str:
    return (f"Claimant: {name}\nPolicy: {policy}\nIncident date: {incident}\n"
            f"Amount: ${amount}\nProcedure: {procedure}\nDiagnosis: {diagnosis}\n")


def _find(label: str, text: str):
    m = re.search(rf"{label}:\s*(.+)", text)
    return m.group(1).strip() if m else None


class FakeLLM:
    """Deterministic stand-in for the Anthropic API.

    `queue` holds one-off items consumed first: an Exception (raised), a str (returned raw),
    or a dict (JSON-encoded). Otherwise a per-task handler builds a plausible answer.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.queue: list = []
        self.extract_confidence = 0.95
        self.validate_result = {"valid": True, "issues": [], "confidence": 0.9}
        self.score_value = 0.1

    @staticmethod
    def task_of(prompt: str) -> str:
        for marker, task in (("Extract the following fields", "extract"), ("Classify the text", "classify"),
                             ("Summarize the text", "summarize"), ("semantic data-quality validator", "validate"),
                             ("Score the record", "score")):
            if marker in prompt:
                return task
        return "unknown"

    def complete(self, model, prompt, max_tokens):
        task = self.task_of(prompt)
        self.calls.append({"model": model, "prompt": prompt, "task": task})
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            text = item if isinstance(item, str) else json.dumps(item)
        else:
            text = json.dumps(getattr(self, f"_{task}")(prompt))
        return LLMResult(text=text, input_tokens=max(len(prompt) // 4, 1), output_tokens=60)

    # ---- default behaviours ----
    def _extract(self, prompt):
        doc = prompt.split("Document:\n", 1)[1]
        amount = _find("Amount", doc)
        vals = {"claimant_name": _find("Claimant", doc), "policy_number": _find("Policy", doc),
                "incident_date": _find("Incident date", doc),
                "claim_amount": amount.replace("$", "") if amount else None,
                "procedure_code": _find("Procedure", doc), "diagnosis_code": _find("Diagnosis", doc),
                "vendor_name": _find("Vendor", doc), "effective_date": _find("Effective", doc),
                "term_months": _find("Term months", doc), "auto_renews": _find("Auto renews", doc)}
        fields = {}
        for line in prompt.split("Fields (name: description):\n", 1)[1].split("\n\n", 1)[0].splitlines():
            key = line[2:].split(":", 1)[0]
            fields[key] = vals.get(key)
            if key in ("medical_records_referenced", "witnesses"):
                fields[key] = []
        return {"fields": fields, "confidence": self.extract_confidence}

    def _classify(self, prompt):
        amt = _find("Amount", prompt.split("Text:\n", 1)[1]) or "0"
        big = float(amt.replace("$", "").replace(",", "")) > 100000
        cats = re.search(r"one of these categories: (.+?)\.", prompt).group(1).split(", ")
        return {"category": cats[-1] if big else cats[0], "confidence": 0.9, "reasoning": "test"}

    def _summarize(self, prompt):
        return {"summary": "A short summary."}

    def _validate(self, prompt):
        return self.validate_result

    def _score(self, prompt):
        return {"score": self.score_value, "rationale": "test"}

    def _unknown(self, prompt):
        return {}


@dataclass
class Stack:
    llm: FakeLLM
    service: object
    app: object
    http: TestClient
    publisher: InMemoryPublisher
    platform: Platform
    gateway: GatewayClient
    sleeps: list
    clock: dict
    breaker: CircuitBreaker
    l1: InMemoryCache
    l2: InMemoryCache


def make_doc(text=None, source=SourceType.EMAIL, source_id="msg-1", pipeline="claims-processing",
             team="claims", mime="text/plain", data: bytes = None) -> IncomingDocument:
    kwargs = dict(source_type=source, source_id=source_id, timestamp=datetime.now(timezone.utc),
                  team=team, pipeline=pipeline, mime_type=mime)
    if data is not None:
        return IncomingDocument.from_bytes(data, **kwargs)
    return IncomingDocument.from_text(text if text is not None else claim_text(), **kwargs)


@pytest.fixture
def stack():
    config = PlatformConfig()
    llm, publisher, sleeps = FakeLLM(), InMemoryPublisher(), []
    metrics = MetricsRegistry()   # one registry shared by gateway and pipelines
    clock = {"t": 1_000_000.0}
    breaker = CircuitBreaker(clock=lambda: clock["t"])
    l1, l2 = InMemoryCache(lambda: clock["t"]), InMemoryCache(lambda: clock["t"])
    cache = TieredCache(l1, l2, config.gateway.l1_ttl_seconds, config.gateway.l2_ttl_seconds)
    accounts = [TeamAccount.create("claims", KEYS["claims"], daily_quota=5000, priority=1,
                                   monthly_budget_usd=15000),
                TeamAccount.create("compliance", KEYS["compliance"], daily_quota=1000, priority=2),
                TeamAccount.create("data", KEYS["data"], daily_quota=3, priority=3)]
    service, auth = build_service(config, llm, accounts, cache=cache, publisher=publisher,
                                  breaker=breaker, metrics=metrics, quota=QuotaManager(lambda: clock["t"]),
                                  sleep=sleeps.append, clock=lambda: clock["t"])
    app = create_app(service, auth)
    http = TestClient(app)
    platform = Platform(config, auth, publisher=publisher, metrics=metrics,
                        context={"today": date(2026, 11, 21),
                                 "policy_lookup": lambda p: {"coverage_limit": 500000} if p == "POL-123456" else None})
    gateway = GatewayClient(http, KEYS["claims"], team="claims", pipeline="claims-processing")
    return Stack(llm, service, app, http, publisher, platform, gateway, sleeps, clock, breaker, l1, l2)


@pytest.fixture
def claims_stack(stack):
    report = stack.platform.onboard(CLAIMS_YAML, stack.gateway)
    assert report.ok, report.failures()
    stack.runner = stack.platform.runners["claims-processing"]
    return stack
