import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace
from src.levers.caching import (ResponseCache, InMemoryCacheBackend, content_hash, cosine,
                                S3CacheBackend, TTL_SECONDS)
from src.levers.model_tiering import ModelRouter, complexity_score, weighted_cost_ratio
from src.levers.prompt_optimization import (build_optimized_prompt, strip_verbose,
                                            token_reduction, estimate_tokens)
from src.levers.batching import BatchAccumulator, BatchItem, BatchSubmitter, is_batchable
from src.cost_tracker import CostTrackingClient
from src.cost_events import InMemorySink
from tests.conftest import FakeAnthropic
from datetime import datetime, timezone


# caching
def test_hash_normalizes_case_and_whitespace():
    assert content_hash("  Why was I CHARGED twice? ") == content_hash("why was i  charged twice?")


def test_cache_ttl_30_days():
    t = [0.0]
    c = ResponseCache(InMemoryCacheBackend(clock=lambda: t[0]))
    c.put("hello", "R")
    t[0] = TTL_SECONDS - 1
    assert c.get("hello") == ("R", "exact")
    t[0] = TTL_SECONDS + 10
    assert c.get("hello") is None
    assert c.hit_rate == 0.5


def test_semantic_dedup_threshold_095():
    vecs = {"a": [1, 0], "near": [1, 0.1], "far": [1, 1]}   # cos(a,near)=0.995, cos(a,far)=0.707
    c = ResponseCache(embedder=lambda s: vecs[s])
    c.put("a", "RESP")
    assert c.get("near") == ("RESP", "semantic")
    assert c.get("far") is None
    assert cosine([1, 0], [0, 1]) == 0


def test_s3_backend_roundtrip_and_miss():
    s3 = MagicMock()
    b = S3CacheBackend("bkt", client=s3)
    b.put("k", "v")
    assert s3.put_object.call_args.kwargs["StorageClass"] == "INTELLIGENT_TIERING"
    class NoSuchKey(Exception): pass
    s3.get_object.side_effect = NoSuchKey()
    assert b.get("k") is None


# tiering
def test_router_tiers():
    r = ModelRouter()
    assert r.route("I can't log in").tier == "haiku"
    mid = "Why was my refund on the transaction disputed? How do I escalate the claim with Acme Corp " + "word " * 40
    assert r.route(mid).tier == "sonnet"
    hard = ("Why did the regulator audit our compliance policy? How is the fraud dispute chargeback "
            "handled by Jane Smith at Acme Bank for invoice 123456 and $4,500 claim 99999? ") + "legal " * 100
    assert r.route(hard).tier == "opus"
    assert r.fallback(r.route("hi")).tier == "sonnet"
    assert r.fallback(r.route(hard)) is None
    assert 0 <= complexity_score(hard) <= 10


def test_article_tier_blend_formulas():
    # (60% x 0.80 + 40% x 3) / 3 -> 0.56 (article claims 0.47)
    assert weighted_cost_ratio(0.6, 0.8, 3, 3) == pytest.approx(0.56)
    # lever description: 80% Haiku, 20% Opus vs Opus: $3.64 vs $15 -> 76% reduction
    assert weighted_cost_ratio(0.8, 0.8, 15, 15) == pytest.approx(3.64 / 15)
    assert 1 - 3.64 / 15 == pytest.approx(0.757, abs=1e-3)


# prompt optimization
def test_prompt_tokens_400_to_280_is_30pct():
    assert token_reduction(400, 280) == 0.30


def test_optimized_prompt_trims_examples_and_shrinks():
    ex = [{"input": f"i{n}", "output": f"o{n}"} for n in range(5)]
    p = build_optimized_prompt("ticket body", examples=ex)
    assert p["system"].count("In:") == 2 and p["user"] == "ticket body"
    verbose = ("Please analyze the following ticket and respond with JSON. "
               "Please provide a detailed explanation.")
    assert estimate_tokens(strip_verbose(verbose)) < estimate_tokens(verbose)


# batching
def test_accumulator_and_latency_sensitive_tasks():
    acc = BatchAccumulator(min_size=2, max_size=3)
    assert not acc.ready()
    acc.add(BatchItem("1", "p", "m"))
    acc.add(BatchItem("2", "p", "m"))
    assert acc.ready()
    assert acc.add(BatchItem("3", "p", "m")) is True
    assert len(acc.drain()) == 3 and acc.items == []
    assert is_batchable("claims_processing") and not is_batchable("fraud_detection")


def test_batch_submit_collect_applies_50pct_discount():
    from src.config import AnthropicConfig
    model = AnthropicConfig().sonnet_model
    msg = SimpleNamespace(model=model, usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
                          content=[SimpleNamespace(text="ok")])
    results = [SimpleNamespace(custom_id="r1", result=SimpleNamespace(type="succeeded", message=msg)),
               SimpleNamespace(custom_id="r2", result=SimpleNamespace(type="errored", message=None))]
    client = MagicMock()
    client.messages.batches.create.return_value = SimpleNamespace(id="b1")
    client.messages.batches.results.return_value = results
    client.messages.batches.retrieve.return_value = SimpleNamespace(processing_status="ended")
    sink = InMemorySink()
    tracker = CostTrackingClient(client=FakeAnthropic(), sink=sink)
    sub = BatchSubmitter(client, tracker, pipeline_name="nightly")
    bid = sub.submit([BatchItem("r1", "p", model), BatchItem("r2", "p", model)])
    out = sub.collect(bid)
    assert out == {"r1": "ok"}
    assert len(client.messages.batches.create.call_args.kwargs["requests"]) == 2
    assert sink.events[0].cost_usd == pytest.approx(1.5)   # $3 x 50%
    assert sink.events[0].batch is True


def test_batch_collect_polls_until_ended_and_rejects_bad_custom_id():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    client = MagicMock()
    client.messages.batches.retrieve.side_effect = [
        SimpleNamespace(processing_status="in_progress"),
        SimpleNamespace(processing_status="in_progress"),
        SimpleNamespace(processing_status="ended"),
    ]
    client.messages.batches.results.return_value = []
    sleeps = []
    sub = BatchSubmitter(client)
    assert sub.collect("b1", sleep=sleeps.append, poll_seconds=5) == {}
    assert sleeps == [5, 5]
    client.messages.batches.retrieve.side_effect = None
    client.messages.batches.retrieve.return_value = SimpleNamespace(processing_status="in_progress")
    with pytest.raises(TimeoutError):
        sub.collect("b2", sleep=lambda s: None, poll_seconds=10, timeout_seconds=20)
    with pytest.raises(ValueError):
        sub.submit([BatchItem(record_id="bad id!", prompt="p", model="m")])
