import pytest
from src.waterfall import (article_waterfall, compute_waterfall, computed_tier_factor,
                           reconcile_waterfall, ticket_worked_example,
                           reconcile_ticket_example, engineering_payback)


def test_article_waterfall_sequence():
    wf = article_waterfall()
    assert [s.cost for s in wf.stages] == pytest.approx([150_000, 90_000, 42_000, 31_500, 15_750])
    assert wf.total_reduction == pytest.approx(0.895)


def test_unrounded_waterfall_is_slightly_different():
    a = article_waterfall().assumptions
    a.round_tier_stage_to = 0
    wf = compute_waterfall(a)
    assert wf.stages[2].cost == pytest.approx(42_300)
    assert wf.final == pytest.approx(15_862.5)


def test_payback_under_one_month():
    p = engineering_payback(article_waterfall())
    assert p["investment_usd"] == 12_000 and p["payback_months"] < 1


def test_tier_factor_discrepancy_detected():
    assert computed_tier_factor() == pytest.approx(0.56)
    wf = compute_waterfall(tier_factor=0.56)
    assert wf.stages[2].cost == pytest.approx(50_000, rel=0.01)
    notes = reconcile_waterfall()
    assert any("0.56" in n and "0.47" in n for n in notes)
    # independent recompute is unrounded: 90,000 x 0.56 = 50,400 -> x0.75 -> x0.5 = 18,900
    assert any("[150000, 90000, 50400, 37800, 18900]" in n and "87.4%" in n for n in notes)


def test_ticket_worked_example_matches_article():
    r = ticket_worked_example()
    assert (r["after_cache"], r["after_tier"], r["after_opt"], r["final_api"]) == (87.0, 48.0, 33.6, 16.8)
    assert r["total_before"] == 416 and r["total_after"] == pytest.approx(316.8)
    assert r["cost_per_ticket_before"] == pytest.approx(0.00232)
    assert r["cost_per_ticket_after"] == pytest.approx(0.000336)
    assert r["api_reduction_pct"] == 85.5 and r["total_reduction_pct"] == 23.8


def test_ticket_example_discrepancies_listed():
    text = "\n".join(reconcile_ticket_example())
    assert "399.20" in text and "$99.20" in text
    assert "633.60" in text and "333.60" in text
    assert "$97" in text
