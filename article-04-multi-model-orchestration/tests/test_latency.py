import pytest

from src.latency import ARTICLE_PROFILES, LatencyRecorder, percentile


def test_percentile_matches_known_values():
    data = list(range(1, 101))
    assert percentile(data, 50) == pytest.approx(50.5)
    assert percentile(data, 95) == pytest.approx(95.05)
    assert percentile(data, 99) == pytest.approx(99.01)
    assert percentile([7], 99) == 7.0


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_recorder_reports_per_pattern():
    r = LatencyRecorder()
    for v in (100, 200, 300, 400, 1000):
        r.record("router", v)
    rep = r.report("router")
    assert rep["count"] == 5 and rep["p50"] == 300 and rep["p99"] > rep["p95"] > rep["p50"]
    assert r.report("chain") == {"count": 0}
    assert "router" in r.report_all()


def test_article_profiles_present():
    assert ARTICLE_PROFILES["router"] == {"p50": 250, "p95": 900, "p99": 1800}
    assert ARTICLE_PROFILES["chain"] == {"p50": 800, "p95": 2500, "p99": 4200}
    assert ARTICLE_PROFILES["fan_out"] == {"p50": 1500, "p95": 2000, "p99": 3000}
    assert ARTICLE_PROFILES["cascade"] == {"p50": 220, "p95": 950, "p99": 2500}


def test_compare_to_article():
    r = LatencyRecorder()
    r.record("cascade", 210)
    cmp = r.compare_to_article("cascade")
    assert cmp["p50"] == {"measured": 210, "article": 220}
