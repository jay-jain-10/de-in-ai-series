import anthropic
import httpx

from src.prompt_runner import PromptRunner
from tests.conftest import FakeClient

GOOD = '{"sentiment": "positive", "confidence": 0.9}'
BAD = '{"sentiment_score": "positive", "confidence": 0.9}'


def test_valid_output_first_try(registry):
    c = FakeClient(lambda kw: GOOD)
    r = PromptRunner(c, registry).run("sentiment_classifier", "Great product")
    assert r.schema_valid and r.attempts == 1 and r.prompt_version == "1.3"
    assert c.calls[0]["system"] == registry.get("sentiment_classifier").system_prompt
    assert c.calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert r.input_tokens == 10 and r.output_tokens == 5


def test_specific_version_is_used(registry):
    c = FakeClient(lambda kw: GOOD)
    r = PromptRunner(c, registry).run("sentiment_classifier", "x", version="1.2")
    assert r.prompt_version == "1.2"
    assert c.calls[0]["system"] == registry.get("sentiment_classifier", "1.2").system_prompt


def test_retry_with_schema_example_after_violation(registry):
    seq = iter([BAD, GOOD])
    c = FakeClient(lambda kw: next(seq))
    r = PromptRunner(c, registry).run("sentiment_classifier", "Great product")
    assert r.schema_valid and r.attempts == 2
    retry_msgs = c.calls[1]["messages"]
    assert retry_msgs[1] == {"role": "assistant", "content": BAD}
    assert "schema" in retry_msgs[2]["content"] and "enum" in retry_msgs[2]["content"]


def test_persistent_violation_returns_invalid_not_garbage(registry):
    c = FakeClient(lambda kw: BAD)
    r = PromptRunner(c, registry).run("sentiment_classifier", "x")
    assert not r.schema_valid and r.output is None and r.attempts == 2
    assert "Contract violation" in r.error and r.comparable() is None
    assert not r.api_failed


def test_api_error_is_captured(registry):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    c = FakeClient(lambda kw: err)
    r = PromptRunner(c, registry).run("sentiment_classifier", "x")
    assert not r.schema_valid and "API error" in r.error
    assert r.api_failed and r.attempts == 2 and len(c.calls) == 2
