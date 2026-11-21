from types import SimpleNamespace

import anthropic
import httpx
import pytest

from src.llm_utils import UsageTracker, call_claude, parse_json, strip_fences
from tests.conftest import fake_response


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx.Response(code, request=req), body=None)


class ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def test_rate_limit_retried_with_exponential_backoff():
    sleeps = []
    client = ScriptedClient([_status_error(anthropic.RateLimitError, 429),
                             _status_error(anthropic.RateLimitError, 429),
                             fake_response("ok", 7, 3)])
    t = UsageTracker()
    assert call_claude(client, "m", "p", 10, t, max_retries=3, sleep=sleeps.append) == "ok"
    assert sleeps == [2, 4] and client.calls == 3
    assert (t.requests, t.input_tokens, t.output_tokens) == (1, 7, 3)


def test_server_error_also_backs_off_then_raises():
    sleeps = []
    client = ScriptedClient([_status_error(anthropic.InternalServerError, 500)] * 3)
    with pytest.raises(anthropic.InternalServerError):
        call_claude(client, "m", "p", 10, UsageTracker(), max_retries=3, sleep=sleeps.append)
    assert client.calls == 3 and sleeps == [2, 4]


def test_strip_fences_and_parse():
    assert strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert parse_json("  {\"a\": 1} ") == {"a": 1}
