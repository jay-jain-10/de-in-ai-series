import datetime as dt

from src.config import GovernanceConfig
from src.monitoring import compute_metrics
from src.prompt_registry import S3RegistryStore
from src.rollback import AutoRollback
from tests.conftest import FakeS3
from tests.test_monitoring import pairs

NOW = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)  # 4 days after 1.3 activation (2026-09-01)


def bad_metrics():
    # pairs() uses current version 1.3 / previous 1.2
    return compute_metrics(pairs(100, diverge=30), "SentimentOutput")


def test_breach_in_window_rolls_back_and_persists(registry, tmp_path):
    s3 = FakeS3()
    store = S3RegistryStore("b", client=s3)
    path = tmp_path / "registry.yaml"
    rb = AutoRollback(registry, store=store, local_path=str(path), now=lambda: NOW)
    d = rb.evaluate_and_rollback(bad_metrics())
    assert d.rolled_back and (d.from_version, d.to_version) == ("1.3", "1.2")
    assert d.within_sla and d.elapsed_seconds < 300
    assert registry.active_version("sentiment_classifier") == "1.2"
    # runtime (S3) and Git-side file both now point at the previous version
    assert store.load().active_version("sentiment_classifier") == "1.2"
    assert "active_version: '1.2'" in path.read_text()


def test_no_breach_no_rollback(registry):
    rb = AutoRollback(registry, now=lambda: NOW)
    d = rb.evaluate_and_rollback(compute_metrics(pairs(100), "SentimentOutput"))
    assert not d.rolled_back and registry.active_version("sentiment_classifier") == "1.3"


def test_warning_only_does_not_roll_back(registry):
    rb = AutoRollback(registry, now=lambda: NOW)
    d = rb.evaluate_and_rollback(compute_metrics(pairs(100, diverge=12), "SentimentOutput"))
    assert not d.rolled_back


def test_breach_after_7_day_window_needs_manual_review(registry):
    late = NOW + dt.timedelta(days=10)
    d = AutoRollback(registry, now=lambda: late).evaluate_and_rollback(bad_metrics())
    assert not d.rolled_back and "monitoring window" in d.reasons[0]
    assert registry.active_version("sentiment_classifier") == "1.3"


def test_window_boundary_is_7_days(registry):
    start = registry.last_activated_at("sentiment_classifier")
    assert AutoRollback(registry, now=lambda: start + dt.timedelta(days=7)).in_monitoring_window("sentiment_classifier")
    assert not AutoRollback(registry, now=lambda: start + dt.timedelta(days=7, seconds=1)).in_monitoring_window("sentiment_classifier")


def test_metrics_for_stale_version_are_ignored(registry):
    registry.rollback("sentiment_classifier")  # already on 1.2; metrics still say 1.3
    d = AutoRollback(registry, now=lambda: NOW).evaluate_and_rollback(bad_metrics())
    assert not d.rolled_back and registry.active_version("sentiment_classifier") == "1.2"
