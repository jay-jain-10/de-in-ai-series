"""Human review queue, feedback loop and calibration protocol."""

from datetime import datetime, timedelta

import pytest

from src.calibration import (
    cohens_kappa, draw_calibration_sample, evaluate_calibration, precision_both_agree,
    precision_recall, suggest_threshold,
)
from src.quarantine import QuarantineRecord
from src.review_queue import ReviewQueue


def add(store, rid, layer="L2", check="L2:semantic_consistency", anomaly="mismatch",
        conf=0.9, sev="critical", ts=None, name="semantic_consistency"):
    store.add(QuarantineRecord(rid, "healthcare_claims", layer, check, name,
                               anomaly_type=anomaly, confidence=conf, severity=sev,
                               flagged_timestamp=ts))


# ---- review queue ----

def test_queue_orders_by_severity_then_confidence(store):
    add(store, "a", layer="L1", check="L1:r", name="r", anomaly=None, conf=None, sev="medium")
    add(store, "b", conf=0.7)
    add(store, "c", conf=0.95)
    q = ReviewQueue(store, sample_rate=1.0)
    assert [r["record_id"] for r in q.pending()] == ["c", "b", "a"]


def test_queue_samples_l1_but_not_l2(store):
    for i in range(400):
        add(store, f"x{i}", layer="L1", check="L1:r", name="r", anomaly=None, conf=None, sev="high")
    for i in range(10):
        add(store, f"y{i}")
    q = ReviewQueue(store, sample_rate=0.10)
    pend = q.pending()
    l1 = [r for r in pend if r["flagged_by_layer"] == "L1"]
    assert 15 < len(l1) < 70
    assert len([r for r in pend if r["flagged_by_layer"] == "L2"]) == 10
    full = ReviewQueue(store, sample_rate=0.10, full_review_sources={"healthcare_claims"})
    assert len(full.pending()) == 410


def test_decisions_logged_and_removed_from_queue(store):
    add(store, "a")
    q = ReviewQueue(store)
    q.submit_decision("a", "L2:semantic_consistency", "confirm_error", "alice", "real")
    row = store.get("a", "L2:semantic_consistency")
    assert row["reviewer_decision"] == "confirm_error" and row["reviewed_by"] == "alice"
    assert row["reviewed_timestamp"] and row["notes"] == "real"
    assert q.pending() == []
    with pytest.raises(KeyError):
        q.submit_decision("nope", "x", "confirm_error", "alice")


def test_sla_backlog_breach(store):
    old = (datetime.utcnow() - timedelta(hours=49)).isoformat(timespec="seconds")
    new = (datetime.utcnow() - timedelta(hours=47)).isoformat(timespec="seconds")
    add(store, "old", ts=old)
    add(store, "new", ts=new)
    assert [r["record_id"] for r in ReviewQueue(store).backlog_breaches()] == ["old"]


def test_feedback_recommendations(store):
    q = ReviewQueue(store)
    for i in range(6):   # L2 anomaly mostly rejected by humans
        add(store, f"fp{i}", anomaly="lab_mismatch")
        q.submit_decision(f"fp{i}", "L2:semantic_consistency", "accept_as_is", "bob")
    for i in range(6):   # L1 rule overridden
        add(store, f"r{i}", layer="L1", check="L1:strict", name="strict", anomaly=None, conf=None)
        q.submit_decision(f"r{i}", "L1:strict", "accept_as_is", "bob")
    for i in range(6):   # confirmed errors -> raise sampling
        add(store, f"ok{i}", anomaly="mismatch")
        q.submit_decision(f"ok{i}", "L2:semantic_consistency", "confirm_error", "bob")
    rep = q.feedback_report()
    actions = {(r["key"], r["action"]) for r in rep["recommendations"]}
    assert ("L2:lab_mismatch", "improve_prompt_examples") in actions
    assert ("L1:strict", "relax_rule") in actions
    assert ("healthcare_claims", "increase_sampling_rate") not in actions  # 6/18 confirmed < 50%


def test_l2_precision_from_decisions(store):
    q = ReviewQueue(store)
    for i, d in enumerate(["confirm_error"] * 17 + ["fix_upstream"] * 0 + ["accept_as_is"] * 3):
        add(store, f"p{i}")
        q.submit_decision(f"p{i}", "L2:semantic_consistency", d, "bob")
    assert q.l2_precision() == 0.85


def test_l2_precision_none_without_reviews(store):
    assert ReviewQueue(store).l2_precision() is None


# ---- calibration ----

def test_kappa_perfect_none_and_known_value():
    a = [True, True, False, False]
    assert cohens_kappa(a, a) == 1.0
    assert cohens_kappa([True, False, True, False], [False, True, False, True]) == -1.0
    # 2x2: both yes 20, both no 15, a-yes/b-no 5, a-no/b-yes 10 -> kappa = 0.4
    ra = [True] * 25 + [False] * 25
    rb = [True] * 20 + [False] * 5 + [True] * 10 + [False] * 15
    assert cohens_kappa(ra, rb) == pytest.approx(0.4, abs=1e-9)


def test_kappa_unbalanced_marginals_by_hand():
    # yes/yes 45, yes/no 15, no/yes 25, no/no 15 (n=100)
    # po = 0.60; pa = 0.60, pb = 0.70; pe = 0.6*0.7 + 0.4*0.3 = 0.54
    # kappa = (0.60 - 0.54) / (1 - 0.54) = 0.130435
    ra = [True] * 60 + [False] * 40
    rb = [True] * 45 + [False] * 15 + [True] * 25 + [False] * 15
    assert cohens_kappa(ra, rb) == pytest.approx(0.06 / 0.46, abs=1e-9)
    # high raw agreement but kappa < 0.7 because of skewed prevalence
    ra = [True] * 90 + [True] * 5 + [False] * 5
    rb = [True] * 90 + [False] * 5 + [True] * 5
    # po = 0.90, pa = pb = 0.95, pe = 0.905 -> kappa = -0.0526
    assert cohens_kappa(ra, rb) == pytest.approx((0.90 - 0.905) / 0.095, abs=1e-9)
    assert not evaluate_calibration("d", ra, rb).kappa_ok


def test_kappa_degenerate_same_label():
    assert cohens_kappa([True] * 5, [True] * 5) == 1.0
    with pytest.raises(ValueError):
        cohens_kappa([], [])
    with pytest.raises(ValueError):
        cohens_kappa([True], [True, False])


def test_precision_requires_both_reviewers():
    a = [True, True, True, False]
    b = [True, False, True, False]
    assert precision_both_agree(a, b) == 0.5


def test_calibration_sample_is_200_and_reproducible():
    flagged = [{"id": i} for i in range(1000)]
    s1 = draw_calibration_sample(flagged, seed=1)
    assert len(s1) == 200 and s1 == draw_calibration_sample(flagged, seed=1)
    assert len(draw_calibration_sample(flagged[:50])) == 50


def test_calibration_report_thresholds():
    good = [True] * 90 + [False] * 10
    rep = evaluate_calibration("healthcare", good, good)
    assert rep.precision == 0.9 and rep.kappa == 1.0 and rep.trustworthy and not rep.actions

    low = [True] * 70 + [False] * 30
    rep = evaluate_calibration("healthcare", low, low)
    assert not rep.precision_ok and rep.kappa_ok
    assert {"add_few_shot_examples", "refine_prompt", "adjust_confidence_threshold"} <= set(rep.actions)

    a = [True, False] * 50
    b = [True, True, False, False] * 25
    rep = evaluate_calibration("healthcare", a, b)
    assert not rep.kappa_ok and not rep.trustworthy
    assert any("standardize" in x for x in rep.actions)


def test_exact_thresholds_inclusive():
    a = [True] * 80 + [False] * 20
    assert evaluate_calibration("d", a, a).precision_ok  # 80% passes


def test_precision_recall_and_threshold_suggestion():
    pr = precision_recall(["a", "b", "c", "d"], ["a", "b", "x"])
    assert pr == {"precision": 0.5, "recall": pytest.approx(0.6667, abs=1e-4), "true_positives": 2}
    scored = ([{"confidence": 0.9, "confirmed": True}] * 9 + [{"confidence": 0.9, "confirmed": False}]
              + [{"confidence": 0.55, "confirmed": False}] * 10)
    assert suggest_threshold(scored) == 0.6
    assert suggest_threshold([{"confidence": 0.9, "confirmed": False}]) is None
