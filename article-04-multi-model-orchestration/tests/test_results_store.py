from src.results_store import (
    FALLBACK_TABLE, HUMAN_REVIEW_TABLE, RESULTS_TABLE, InMemoryResultStore, idempotency_key,
    merge_sql, route_table, to_row,
)
from src.schemas import TaskResult


def tr(rid="r1", ver="v1", task="sentiment", conf=0.9, fb=False, result=None):
    return TaskResult(rid, task, "haiku", ver, result or {"sentiment": "positive"}, conf, fallback=fb)


def test_composite_key():
    assert idempotency_key("r1", "v1", "sentiment") == ("r1", "v1", "sentiment")
    assert idempotency_key("r1", "v1", "sentiment") != idempotency_key("r1", "v2", "sentiment")


def test_rerun_is_idempotent():
    s = InMemoryResultStore()
    rows = [to_row(tr()), to_row(tr(rid="r2"))]
    assert s.merge(rows) == {"inserted": 2, "updated": 0}
    assert s.merge(rows) == {"inserted": 0, "updated": 2}
    assert s.count(RESULTS_TABLE) == 2


def test_new_model_version_adds_row_same_version_updates():
    s = InMemoryResultStore()
    s.merge([to_row(tr(ver="1.3"))])
    s.merge([to_row(tr(ver="1.4"))])
    assert s.count(RESULTS_TABLE) == 2
    s.merge([to_row(tr(ver="1.4", conf=0.5))])
    assert s.count(RESULTS_TABLE) == 2
    assert [r["confidence"] for r in s.rows(RESULTS_TABLE) if r["model_version"] == "1.4"] == [0.5]


def test_same_review_different_task_not_collapsed():
    s = InMemoryResultStore()
    s.merge([to_row(tr(task="sentiment")), to_row(tr(task="aspect_extraction"))])
    assert s.count(RESULTS_TABLE) == 2


def test_routing_by_data():
    assert route_table(to_row(tr(fb=True))) == FALLBACK_TABLE
    assert route_table(to_row(tr(task="toxicity_detection", conf=0.69))) == HUMAN_REVIEW_TABLE
    assert route_table(to_row(tr(task="toxicity_detection", conf=0.7))) == RESULTS_TABLE
    assert route_table(to_row(tr(task="sentiment", conf=0.1))) == RESULTS_TABLE


def test_rerun_moves_row_out_of_fallback_table():
    s = InMemoryResultStore()
    s.merge([to_row(tr(fb=True, conf=0.0))])
    assert s.count(FALLBACK_TABLE) == 1 and s.fallback_rate() == 1.0
    s.merge([to_row(tr())])
    assert s.count(FALLBACK_TABLE) == 0 and s.count(RESULTS_TABLE) == 1


def test_merge_sql_uses_composite_key():
    sql = merge_sql("sentiment_results")
    assert "MERGE INTO sentiment_results" in sql
    assert "t.review_id = s.review_id AND t.model_version = s.model_version AND t.task_type = s.task_type" in sql


def test_merge_sql_rejects_non_identifier_table_names():
    import pytest
    assert "MERGE INTO analytics.reviews.review_results" in merge_sql("analytics.reviews.review_results")
    for bad in ("x; DROP TABLE y", "t --", "a b", ""):
        with pytest.raises(ValueError):
            merge_sql(bad)
