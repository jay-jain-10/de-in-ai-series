"""Shared test doubles: fake clock, stub Claude client, event builders."""

import json
from unittest.mock import MagicMock

import anthropic
import httpx


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s: float):
        self.t += s


def make_event(i=0, user="u1", amount=50.0, ts=1_800_000_000.0, **kw):
    d = {"event_id": f"e{i}", "user_id": user, "amount": amount, "event_ts": ts}
    d.update(kw)
    return d


def claude_response(items, in_tok=1000, out_tok=100):
    r = MagicMock()
    r.content = [MagicMock(text=json.dumps(items))]
    r.usage.input_tokens = in_tok
    r.usage.output_tokens = out_tok
    return r


def scoring_client(score=50, in_tok_per_event=500, out_tok_per_event=40):
    """Mock anthropic client that answers every event in the prompt."""
    client = MagicMock()

    def create(**kw):
        events = json.loads(kw["messages"][0]["content"].split("Events:\n", 1)[1])
        items = [{"event_id": e["event_id"], "risk_score": score, "anomaly_type": "account_takeover",
                  "confidence": 0.9, "reasoning": "stub"} for e in events]
        return claude_response(items, in_tok_per_event * len(events), out_tok_per_event * len(events))

    client.messages.create.side_effect = create
    return client


def rate_limit_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


def connection_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
