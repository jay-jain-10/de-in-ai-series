"""Dagster asset graph, retry policies, and the model-aware I/O manager."""

import dagster as dg
import pytest

from dags.review_enrichment_assets import (
    ALL_ASSETS, PipelineResources, ReviewResultsIOManager, build_definitions, daily_schedule,
    run_concurrently,
)
from src.results_store import FALLBACK_TABLE, HUMAN_REVIEW_TABLE, RESULTS_TABLE, InMemoryResultStore
from src.router import ModelRouter
from tests.conftest import FakeAnthropic, make_stack

REVIEWS = [
    {"review_id": "pos", "text": "Love it, wonderful battery."},
    {"review_id": "neg", "text": "Terrible battery, software is buggy."},
    {"review_id": "toxic", "text": "Terrible battery, support guy is an idiot."},
]


def run(config, handler=None, reviews=REVIEWS):
    client = FakeAnthropic(handler)
    gw, _, _ = make_stack(client, config)
    store = InMemoryResultStore()
    pipeline = PipelineResources(gw, ModelRouter(config), lambda: reviews)
    defs = build_definitions(pipeline, store)
    result = dg.materialize(
        ALL_ASSETS,
        resources={"pipeline": dg.ResourceDefinition.hardcoded_resource(pipeline),
                   "io_manager": ReviewResultsIOManager(store)},
    )
    return result, client, store


def test_asset_graph_dependencies():
    keys = {a.key.to_user_string(): a for a in ALL_ASSETS}
    assert set(keys) == {"review_text", "sentiment_classifications", "aspect_classifications",
                         "toxicity_classifications", "final_review_enrichment"}
    defs = build_definitions(None, InMemoryResultStore())
    graph = defs.resolve_asset_graph()
    parents = lambda k: {p.to_user_string() for p in graph.get(dg.AssetKey(k)).parent_keys}
    assert parents("sentiment_classifications") == {"review_text"}
    assert parents("aspect_classifications") == {"review_text", "sentiment_classifications"}
    assert parents("toxicity_classifications") == {"review_text", "aspect_classifications"}
    assert parents("final_review_enrichment") == {
        "review_text", "sentiment_classifications", "aspect_classifications", "toxicity_classifications"}


def test_retry_policies_match_article():
    by = {a.key.to_user_string(): a for a in ALL_ASSETS}
    retries = {k: v.op.retry_policy.max_retries for k, v in by.items() if v.op.retry_policy}
    assert retries == {"sentiment_classifications": 2, "aspect_classifications": 1,
                       "toxicity_classifications": 1, "final_review_enrichment": 3}


def test_schedule_is_daily_2am():
    assert daily_schedule.cron_schedule == "0 2 * * *"


def test_definitions_validate(config):
    gw, _, _ = make_stack(FakeAnthropic(), config)
    defs = build_definitions(PipelineResources(gw, ModelRouter(config), lambda: []), InMemoryResultStore())
    assert defs.resolve_job_def("daily_review_enrichment") is not None


def test_end_to_end_conditional_execution(config):
    result, client, store = run(config)
    assert result.success
    sent = result.output_for_node("sentiment_classifications")
    asp = result.output_for_node("aspect_classifications")
    tox = result.output_for_node("toxicity_classifications")
    assert len(sent) == 3
    assert {a.review_id for a in asp} == {"neg", "toxic"}   # positive review skipped
    assert {t.review_id for t in tox} == {"toxic"}          # only flagged aspects
    assert len(client.calls_for(task="toxicity_detection")) == 1
    assert "opus" in client.calls_for(task="toxicity_detection")[0]["model"]


def test_final_enrichment_aggregates_cost_and_lineage(config):
    result, _, _ = run(config)
    rows = {r["review_id"]: r for r in result.output_for_node("final_review_enrichment")}
    assert rows["pos"]["aspects"] is None and rows["pos"]["total_cost_usd"] > 0
    assert rows["toxic"]["toxic"] is True
    assert [l["task"] for l in rows["toxic"]["lineage"]] == [
        "sentiment", "aspect_extraction", "toxicity_detection"]
    assert rows["toxic"]["total_cost_usd"] > rows["neg"]["total_cost_usd"] > rows["pos"]["total_cost_usd"]
    assert not rows["pos"]["needs_human_review"]


def test_io_manager_routes_low_confidence_toxicity_to_human_review(config):
    def handler(model, task, text):
        if task == "toxicity_detection":
            return {"toxic": True, "categories": [], "confidence": 0.6, "reasoning": "unsure"}

    result, _, store = run(config, handler)
    assert store.count(HUMAN_REVIEW_TABLE) == 1
    assert store.rows(HUMAN_REVIEW_TABLE)[0]["review_id"] == "toxic"
    final = {r["review_id"]: r for r in result.output_for_node("final_review_enrichment")}
    assert final["toxic"]["needs_human_review"] is True
    assert store.count(RESULTS_TABLE) == 5  # 3 sentiment + 2 aspect


def test_io_manager_quarantines_fallbacks_and_blocks_downstream(config):
    def handler(model, task, text):
        if task == "sentiment" and "Terrible" in text:
            return "garbage"

    result, client, store = run(config, handler)
    assert store.count(FALLBACK_TABLE) == 2
    assert client.calls_for(task="aspect_extraction") == []  # failed sentiment -> no downstream
    final = {r["review_id"]: r for r in result.output_for_node("final_review_enrichment")}
    assert final["neg"]["needs_human_review"] and final["neg"]["any_fallback"]


def test_rerun_is_idempotent_in_store(config):
    _, _, store = run(config)
    n = sum(store.count(t) for t in store.TABLES)
    # second run into the same store
    client = FakeAnthropic()
    gw, _, _ = make_stack(client, config)
    pipeline = PipelineResources(gw, ModelRouter(config), lambda: REVIEWS)
    dg.materialize(ALL_ASSETS, resources={
        "pipeline": dg.ResourceDefinition.hardcoded_resource(pipeline),
        "io_manager": ReviewResultsIOManager(store)})
    assert sum(store.count(t) for t in store.TABLES) == n


def test_run_concurrently_bounds_parallelism_and_keeps_order():
    import threading, time
    active, peak, lock = [0], [0], threading.Lock()

    def fn(x):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return x * 2

    assert run_concurrently(fn, list(range(20)), max_concurrency=3) == [i * 2 for i in range(20)]
    assert peak[0] <= 3
    assert run_concurrently(fn, [], 3) == []
