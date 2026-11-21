import pytest
from src.config import CostSLAConfig
from src.cost_events import CostEvent
from src.cost_sla import check_record_sla, check_daily_sla, check_variance, SLAEnforcer


def test_article_sla_defaults():
    s = CostSLAConfig()
    assert (s.max_cost_per_record_usd, s.max_daily_cost_usd, s.variance_threshold) == (0.005, 200, 2.0)


def test_record_sla_boundaries():
    assert check_record_sla(0.005) is None
    assert check_record_sla(0.0051).kind == "per_record"


def test_daily_sla_boundaries():
    assert check_daily_sla(200) is None
    assert check_daily_sla(200.01).kind == "daily"


def test_variance_article_example_150_vs_600():
    v = check_variance(600, [150] * 7)
    assert v and v.kind == "variance" and v.limit == 300
    assert check_variance(300, [150] * 7) is None   # exactly 2x does not alert
    assert check_variance(100, []) is None


def test_enforcer_flags_record_once_including_retries():
    enf = SLAEnforcer()
    e1 = CostEvent("p", "m", 1, 1, 0.003, record_id="r")
    e2 = CostEvent("p", "m", 1, 1, 0.003, record_id="r", attempt=2, is_retry=True)
    e3 = CostEvent("p", "m", 1, 1, 0.003, record_id="r", attempt=3, is_retry=True)
    assert enf.observe(e1) == []
    assert len(enf.observe(e2)) == 1      # 0.006 > 0.005 only because of the retry
    assert enf.observe(e3) == []          # not re-reported
    assert enf.daily_spend("p", e1.created_date) == pytest.approx(0.009)
