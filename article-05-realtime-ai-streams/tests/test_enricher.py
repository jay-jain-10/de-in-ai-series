import json
from unittest.mock import MagicMock

import pytest

from src.enricher import AIEnricher, EnrichmentUnavailable
from src.models import Event
from src.resilience import CircuitBreaker, backoff_delay
from tests.helpers import (FakeClock, claude_response, connection_error, rate_limit_error, scoring_client)

MODEL = "claude-haiku-4-5-20251001"


def items(n=3):
    return [(Event(event_id=f"e{i}", user_id="u", amount=10, event_ts=0), {"typical_amount": 100}) for i in range(n)]


def good(ids, score=60):
    return claude_response([{"event_id": i, "risk_score": score, "anomaly_type": "none",
                             "confidence": 0.8, "reasoning": "ok"} for i in ids])


def test_single_call_per_batch():
    client = scoring_client()
    out = AIEnricher(client, MODEL, sleep=lambda s: None).enrich_batch(items(10))
    assert len(out) == 10 and client.messages.create.call_count == 1
    assert client.messages.create.call_args.kwargs["model"] == MODEL


def test_markdown_wrapped_json_and_bad_item_isolated():
    client = MagicMock()
    r = MagicMock()
    body = [{"event_id": "e0", "risk_score": 70, "anomaly_type": "x", "confidence": 0.5},
            {"event_id": "e1", "risk_score": 999, "anomaly_type": "x", "confidence": 0.5},  # invalid
            {"event_id": "ghost", "risk_score": 10, "anomaly_type": "x", "confidence": 0.5}]  # unknown
    r.content = [MagicMock(text="```json\n" + json.dumps(body) + "\n```")]
    r.usage.input_tokens = r.usage.output_tokens = 10
    client.messages.create.return_value = r
    out = AIEnricher(client, MODEL, sleep=lambda s: None).enrich_batch(items(2))
    assert set(out) == {"e0"}


def test_retry_on_rate_limit_then_success():
    client = MagicMock()
    client.messages.create.side_effect = [rate_limit_error(), connection_error(), good(["e0"])]
    sleeps = []
    out = AIEnricher(client, MODEL, sleep=sleeps.append).enrich_batch(items(1))
    assert "e0" in out and client.messages.create.call_count == 3 and len(sleeps) == 2


def test_exhausted_retries_raise_and_count_failure():
    client = MagicMock()
    client.messages.create.side_effect = rate_limit_error()
    e = AIEnricher(client, MODEL, sleep=lambda s: None)
    with pytest.raises(EnrichmentUnavailable):
        e.enrich_batch(items(1))
    assert client.messages.create.call_count == 3


def test_invalid_json_retries_then_unavailable():
    client = MagicMock()
    r = MagicMock()
    r.content = [MagicMock(text="not json")]
    r.usage.input_tokens = r.usage.output_tokens = 1
    client.messages.create.return_value = r
    with pytest.raises(EnrichmentUnavailable):
        AIEnricher(client, MODEL, sleep=lambda s: None).enrich_batch(items(1))


def test_circuit_breaker_opens_after_three_failed_calls_for_60s():
    clock = FakeClock()
    client = MagicMock()
    client.messages.create.side_effect = rate_limit_error()
    e = AIEnricher(client, MODEL, max_retries=1, breaker=CircuitBreaker(3, 60, clock), sleep=lambda s: None)
    for _ in range(3):
        with pytest.raises(EnrichmentUnavailable):
            e.enrich_batch(items(1))
    calls = client.messages.create.call_count
    with pytest.raises(EnrichmentUnavailable, match="breaker"):
        e.enrich_batch(items(1))
    assert client.messages.create.call_count == calls  # short-circuited
    clock.advance(61)
    client.messages.create.side_effect = [good(["e0"])]
    assert "e0" in e.enrich_batch(items(1))


def test_backoff_has_jitter_and_cap():
    assert backoff_delay(2, rng=lambda: 1.0) == pytest.approx(0.4)
    assert backoff_delay(2, rng=lambda: 0.0) == 0
    assert backoff_delay(30, cap=30, rng=lambda: 1.0) == 30


def test_breaker_counts_each_failed_call_with_default_retries():
    """Article: three API calls failing in a row open the breaker - not three batches."""
    clock = FakeClock()
    client = MagicMock()
    client.messages.create.side_effect = rate_limit_error()
    e = AIEnricher(client, MODEL, breaker=CircuitBreaker(3, 60, clock), sleep=lambda s: None)
    with pytest.raises(EnrichmentUnavailable):
        e.enrich_batch(items(1))
    assert client.messages.create.call_count == 3 and e.breaker.is_open
    with pytest.raises(EnrichmentUnavailable, match="breaker"):
        e.enrich_batch(items(1))
    assert client.messages.create.call_count == 3


def test_retries_stop_when_half_open_trial_fails():
    clock = FakeClock()
    client = MagicMock()
    client.messages.create.side_effect = rate_limit_error()
    sleeps = []
    e = AIEnricher(client, MODEL, breaker=CircuitBreaker(3, 60, clock), sleep=sleeps.append)
    with pytest.raises(EnrichmentUnavailable):
        e.enrich_batch(items(1))
    clock.advance(61)
    before = client.messages.create.call_count
    with pytest.raises(EnrichmentUnavailable, match="breaker"):
        e.enrich_batch(items(1))
    assert client.messages.create.call_count == before + 1  # one trial, no retry storm
