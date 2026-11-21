"""Risk-based sampling: tiers, rates, determinism, cost math."""

import pytest

from src.risk_sampler import (
    RiskBasedSampler, SourceStats, compute_source_risk_score, estimate_monthly_cost,
    tier_for_score, tier_for_source,
)
from src.config import SAMPLING_RATES


def recs(n, unit="OUTPATIENT"):
    return [{"claim_id": f"C{i}", "care_unit": unit} for i in range(n)]


@pytest.mark.parametrize("score,tier", [(95, "high"), (80.01, "high"), (80, "medium"),
                                        (50, "medium"), (49.9, "low"), (0, "low")])
def test_tier_boundaries(score, tier):
    assert tier_for_score(score) == tier


def test_rates_match_article():
    assert SAMPLING_RATES == {"high": 1.0, "medium": 0.20, "low": 0.03}


def test_score_is_capped_and_monotonic():
    lo = compute_source_risk_score(SourceStats("a", 1))
    hi = compute_source_risk_score(SourceStats("a", 5))
    assert lo < hi
    assert compute_source_risk_score(SourceStats("a", 1000, 5, 5)) == 100.0


def test_high_tier_samples_everything():
    s = RiskBasedSampler(salt="x")
    assert len(s.sample(recs(500), "high")) == 500


def test_medium_and_low_rates_approximate():
    s = RiskBasedSampler(salt="x")
    n = 20000
    assert abs(len(s.sample(recs(n), "medium")) / n - 0.20) < 0.015
    assert abs(len(s.sample(recs(n), "low")) / n - 0.03) < 0.01


def test_sampling_is_deterministic_for_retries():
    a = RiskBasedSampler(salt="2026-06-01").sample(recs(2000), "medium")
    b = RiskBasedSampler(salt="2026-06-01").sample(recs(2000), "medium")
    assert a == b
    c = RiskBasedSampler(salt="2026-06-02").sample(recs(2000), "medium")
    assert a != c


def test_icu_and_emergency_always_included():
    rows = recs(1000, "ICU") + recs(1000, "EMERGENCY")
    for r in rows:
        r["claim_id"] = "Z" + r["claim_id"] + r["care_unit"]
    assert len(RiskBasedSampler().sample(rows, "low")) == 2000


def test_safety_critical_always_high_tier():
    assert tier_for_source(SourceStats("s", 0.0), "safety_critical") == "high"


def test_degradation_bumps_tier():
    stable = SourceStats("s", 2.0, 3, 3, baseline_error_rate_per_1000=2.0)
    spiked = SourceStats("s", 5.0, 3, 3, baseline_error_rate_per_1000=2.0)
    assert tier_for_source(stable) == "low"
    assert tier_for_source(spiked) == "medium"


def test_article_cost_math():
    c = estimate_monthly_cost({"high": 1_000_000, "medium": 4_000_000, "low": 5_000_000})
    assert c["cost_by_tier"] == {"high": 3000.0, "medium": 2400.0, "low": 450.0}
    assert c["total_cost"] == 5850.0
    assert c["full_validation_cost"] == 30000.0
    assert c["cost_per_record_avg"] == pytest.approx(0.000585)


def test_cost_math_rejects_unknown_tier():
    with pytest.raises(ValueError):
        estimate_monthly_cost({"high": 10, "critical": 5})


def test_score_formula_by_hand():
    # docstring example: 10/1000 x 3 x 3 x 0.8 = 72 -> medium
    s = compute_source_risk_score(SourceStats("a", 10, 3, 3))
    assert s == 72.0 and tier_for_score(s) == "medium"
