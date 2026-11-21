import sqlite3

import pytest

from src.enrichment_store import InMemoryEnrichmentStore, PostgresEnrichmentStore
from src.models import EnrichmentRecord


def rec(event="e1", version=1, score=50):
    return EnrichmentRecord(event_id=event, enrichment_version=version, user_id="u", risk_score=score,
                            anomaly_type="none", confidence=0.9, enriched_at=1.0)


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    if request.param == "memory":
        return InMemoryEnrichmentStore()
    s = PostgresEnrichmentStore(sqlite3.connect(":memory:"), paramstyle="?")
    s.create_schema()
    return s


def test_retry_five_times_leaves_one_row(store):
    for _ in range(5):
        store.upsert(rec())
    assert store.count() == 1


def test_replay_overwrites_same_key(store):
    store.upsert(rec(score=40))
    store.upsert(rec(score=85))
    assert store.latest("e1").risk_score == 85 and store.count() == 1


def test_new_version_is_separate_row_and_latest_wins(store):
    store.upsert(rec(version=2, score=70))
    store.upsert(rec(version=1, score=10))  # stale version arriving late
    assert store.count() == 2
    assert store.latest("e1").enrichment_version == 2 and store.latest("e1").risk_score == 70


def test_exists(store):
    assert not store.exists("e1", 1)
    store.upsert(rec())
    assert store.exists("e1", 1) and not store.exists("e1", 2)
    assert store.latest("nope") is None
