import json
import pytest
from unittest.mock import MagicMock
from tests.conftest import FakeAnthropic, make_response
from src.config import AnthropicConfig, CostSLAConfig
from src.cost_events import InMemorySink
from src.cost_sla import SLAEnforcer
from src.cost_tracker import CostTrackingClient
from src.metrics import MetricEmitter
from src.pipeline import CostAwarePipeline
from src import lambda_handler

M = AnthropicConfig()


def make_pipe(responses, enforcer=None):
    fake = FakeAnthropic(responses)
    sink = InMemorySink()
    em = MetricEmitter(client=MagicMock())
    tracker = CostTrackingClient(client=fake, sink=sink, metrics=em, enforcer=enforcer)
    return CostAwarePipeline(tracker), fake, sink, em


def test_cache_hit_costs_nothing_and_skips_api():
    pipe, fake, sink, em = make_pipe([make_response('{"a": 1}')])
    r1 = pipe.process("1", "Card declined on file")
    r2 = pipe.process("2", "  card DECLINED on file ")
    assert len(fake.calls) == 1
    assert r1["cost_usd"] > 0 and r2["cost_usd"] == 0 and r2["cost_cache_hit"] is True
    assert r2["model_used"] == "cache:exact"
    assert em.cache_hit_rate == 0.5


def test_haiku_failure_falls_back_to_sonnet_and_counts_retry():
    pipe, fake, sink, em = make_pipe([make_response("garbage"), make_response('{"ok": true}')])
    r = pipe.process("1", "I can't log in")
    assert [c["model"] for c in fake.calls] == [M.haiku_model, M.sonnet_model]
    assert r["success"] and r["cost_attempts"] == 2
    assert [e.is_retry for e in sink.events] == [False, True]
    assert em.token_waste_rate > 0
    assert fake.calls[0]["system"].startswith("Respond with JSON only")


def test_unrecoverable_failure_not_cached():
    pipe, fake, *_ = make_pipe([make_response("bad"), make_response("bad")])
    r = pipe.process("1", "I can't log in")
    assert r["success"] is False and r["result"] is None
    assert pipe.cache.get("I can't log in") is None


def _factory(pipe_responses, enforcer=None):
    pipe, fake, sink, em = make_pipe(pipe_responses, enforcer)
    lambda_handler.set_pipeline_factory(lambda ev: (pipe, em))
    return em


def test_lambda_handler_success_and_metrics_flush():
    em = _factory([make_response('{"x": 1}', 400, 50)])
    out = lambda_handler.handler({"records": [{"record_id": "a", "text": "hello"}]})
    lambda_handler.set_pipeline_factory(None)
    body = json.loads(out["body"])
    assert out["statusCode"] == 200
    assert body["total_cost_tokens"] == 450 and body["total_cost_usd"] > 0
    em.client.put_metric_data.assert_called()


def test_lambda_handler_400_and_throttle_429():
    _factory([])
    assert lambda_handler.handler({"records": []})["statusCode"] == 400
    enf = SLAEnforcer(CostSLAConfig(max_daily_cost_usd=0.0))
    _factory([make_response(), make_response()], enf)
    out = lambda_handler.handler({"records": [{"record_id": "a", "text": "one"},
                                              {"record_id": "b", "text": "two"}]})
    lambda_handler.set_pipeline_factory(None)
    assert out["statusCode"] == 429       # first call passes (spend 0 >= 0 -> blocks immediately)
    assert json.loads(out["body"])["rows"][0]["error"] == "budget_exceeded"


def test_lambda_factory_failure_returns_500():
    lambda_handler.set_pipeline_factory(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
    out = lambda_handler.handler({"records": [{"record_id": "a", "text": "t"}]})
    lambda_handler.set_pipeline_factory(None)
    assert out["statusCode"] == 500
