"""Shared offline fixtures: fake Anthropic client, fake S3, golden oracle."""

import json
import os
import sys
import types
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# snowflake-connector is heavy; tests never need the real one.
try:  # pragma: no cover
    import snowflake.connector  # noqa: F401
except ImportError:  # pragma: no cover
    sf = types.ModuleType("snowflake")
    sfc = types.ModuleType("snowflake.connector")
    sfc.connect = lambda **kw: (_ for _ in ()).throw(RuntimeError("no real snowflake in tests"))
    sf.connector = sfc
    sys.modules["snowflake"] = sf
    sys.modules["snowflake.connector"] = sfc

from src.golden import load_golden  # noqa: E402
from src.prompt_registry import PromptRegistry  # noqa: E402


class FakeClient:
    """Stands in for anthropic.Anthropic. ``responder(kwargs) -> str | Exception``."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.responder(kwargs)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(
            content=[SimpleNamespace(text=out)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


class FakeS3:
    """Minimal in-memory versioned S3."""

    def __init__(self):
        self.objects = {}
        self.versions = 0

    def put_object(self, Bucket, Key, Body, **kw):
        self.versions += 1
        self.objects[(Bucket, Key)] = Body
        return {"VersionId": f"v{self.versions}"}

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def user_text(kwargs) -> str:
    """The original input (first user message)."""
    return kwargs["messages"][0]["content"]


def oracle_json(prompt_name: str, case) -> str:
    """What a perfect model would answer for a golden case."""
    exp = dict(case.expected)
    conf = max(case.min_confidence or 0.0, 0.9)
    if case.acceptable_confidence_range:
        lo, hi = case.acceptable_confidence_range
        conf = (lo + hi) / 2
    if prompt_name == "sentiment_classifier":
        exp.setdefault("sentiment", "negative")
        return json.dumps({"sentiment": exp["sentiment"], "confidence": conf})
    if prompt_name == "escalation_classifier":
        risk = case.expected_approx["escalation_risk"][0]
        return json.dumps({"escalation_risk": risk, "urgency": exp.get("urgency", "high"), "confidence": conf})
    if prompt_name == "vendor_extractor":
        return json.dumps({**exp, "confidence": conf})
    raise KeyError(prompt_name)


def golden_path(name):
    for ext in (".json", ".yaml"):
        p = os.path.join(ROOT, "golden", name + ext)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(name)


@pytest.fixture
def registry():
    return PromptRegistry.from_file(os.path.join(ROOT, "prompts", "registry.yaml"))


@pytest.fixture
def sentiment_cases():
    return load_golden(golden_path("sentiment_classifier"))


@pytest.fixture
def perfect_client(sentiment_cases):
    """Answers every sentiment golden case correctly."""
    by_input = {c.input: c for c in sentiment_cases}

    def responder(kw):
        return oracle_json("sentiment_classifier", by_input[user_text(kw)])

    return FakeClient(responder)
