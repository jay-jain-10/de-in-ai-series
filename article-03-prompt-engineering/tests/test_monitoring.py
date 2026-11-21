from unittest.mock import MagicMock

import pytest

from src.config import GovernanceConfig
from src.monitoring import (
    MetricsPublisher, compute_metrics, confidence_histogram, evaluate_alerts, sample_requests,
)
from src.output_contracts import SentimentOutput, VendorExtraction
from src.prompt_runner import RunResult


def sres(label, conf, version="1.3", valid=True):
    out = SentimentOutput(sentiment=label, confidence=conf) if valid else None
    return RunResult("sentiment_classifier", version, "m", "txt", output=out, schema_valid=valid)


def vres(name, source, version="1.0"):
    out = VendorExtraction(vendor_name=name, terms_days=30, currency="USD", confidence=0.9)
    return RunResult("vendor_extractor", version, "m", source, output=out, schema_valid=True)


def pairs(n, diverge=0, cur_conf=0.9, prev_conf=0.9, invalid=0):
    ps = []
    for i in range(n):
        prev = sres("positive", prev_conf, "1.2")
        if i < invalid:
            cur = sres("positive", cur_conf, valid=False)
        else:
            cur = sres("negative" if i < diverge else "positive", cur_conf)
        ps.append((cur, prev))
    return ps


def test_sampling_is_about_one_percent_and_deterministic():
    recs = [{"input_id": f"t{i}"} for i in range(20000)]
    s1 = sample_requests(recs, 0.01)
    assert 100 <= len(s1) <= 300
    assert s1 == sample_requests(recs, 0.01)


def test_histogram_bins():
    assert confidence_histogram([0.0, 0.05, 0.95, 1.0, 0.55]) == [2, 0, 0, 0, 0, 1, 0, 0, 0, 2]


def test_healthy_metrics_no_alerts():
    m = compute_metrics(pairs(100), "SentimentOutput")
    assert m.divergence_rate == 0 and m.schema_compliance == 1.0 and m.confidence_drop == pytest.approx(0)
    assert m.histogram_current[9] == 100 and m.histogram_previous[9] == 100
    assert evaluate_alerts(m) == []


def test_divergence_warn_then_alert():
    warn = evaluate_alerts(compute_metrics(pairs(100, diverge=12), "SentimentOutput"))
    assert [(a.metric, a.level) for a in warn] == [("divergence", "warn")] and not warn[0].triggers_rollback
    alert = evaluate_alerts(compute_metrics(pairs(100, diverge=16), "SentimentOutput"))
    assert alert[0].level == "alert" and alert[0].triggers_rollback


def test_divergence_at_10_percent_is_quiet():
    assert evaluate_alerts(compute_metrics(pairs(100, diverge=10), "SentimentOutput")) == []


def test_confidence_degradation_alert():
    m = compute_metrics(pairs(50, cur_conf=0.80, prev_conf=0.90), "SentimentOutput")
    assert m.confidence_drop == pytest.approx(0.10)
    assert any(a.metric == "confidence_drop" and a.triggers_rollback for a in evaluate_alerts(m))


def test_schema_compliance_warn_and_alert():
    warn = evaluate_alerts(compute_metrics(pairs(100, invalid=7), "SentimentOutput"))
    assert any(a.metric == "schema_compliance" and a.level == "warn" for a in warn)
    alert = evaluate_alerts(compute_metrics(pairs(100, invalid=11), "SentimentOutput"))
    assert any(a.metric == "schema_compliance" and a.level == "alert" and a.triggers_rollback for a in alert)


def test_hallucination_rate_alert():
    ps = []
    for i in range(100):
        src = "Invoice from Acme Corp"
        cur = vres("Made Up Inc" if i < 6 else "Acme Corp", src, "1.1")
        prev = vres("Acme Corp", src)
        ps.append((cur, prev))
    m = compute_metrics(ps, "VendorExtraction")
    assert m.hallucination_rate == pytest.approx(0.06) and m.hallucination_rate_previous == 0
    a = [x for x in evaluate_alerts(m) if x.metric == "hallucination_rate"]
    assert a and a[0].triggers_rollback


def test_hallucination_delta_over_3_points_triggers_even_below_5_percent():
    ps = []
    for i in range(100):
        src = "Invoice from Acme Corp"
        ps.append((vres("Nope" if i < 4 else "Acme Corp", src, "1.1"), vres("Acme Corp", src)))
    m = compute_metrics(ps, "VendorExtraction")
    assert m.hallucination_rate == pytest.approx(0.04)
    assert any(a.metric == "hallucination_rate" and a.triggers_rollback for a in evaluate_alerts(m))


def test_no_grounded_fields_means_zero_hallucination():
    assert compute_metrics(pairs(10), "SentimentOutput").hallucination_rate == 0.0


def test_empty_sample_rejected():
    with pytest.raises(ValueError):
        compute_metrics([], "SentimentOutput")


def test_publisher_sends_cloudwatch_metrics_and_sns_alerts():
    cw, sns = MagicMock(), MagicMock()
    pub = MetricsPublisher(cloudwatch=cw, sns=sns, topic_arn="arn:aws:sns:us-east-1:1:t")
    m = compute_metrics(pairs(100, diverge=20), "SentimentOutput")
    pub.publish_metrics(m)
    kw = cw.put_metric_data.call_args.kwargs
    assert kw["Namespace"] == "PromptGovernance"
    names = {d["MetricName"] for d in kw["MetricData"]}
    assert {"OutputDivergence", "ConfidenceDrop", "SchemaCompliance", "HallucinationRate"} <= names
    div = next(d for d in kw["MetricData"] if d["MetricName"] == "OutputDivergence")
    assert div["Value"] == pytest.approx(20.0) and div["Unit"] == "Percent"
    pub.send_alerts(m, evaluate_alerts(m))
    assert sns.publish.call_count == 1
    assert "divergence" in sns.publish.call_args.kwargs["Message"]


def test_publisher_skips_sns_when_only_warnings():
    cw, sns = MagicMock(), MagicMock()
    pub = MetricsPublisher(cloudwatch=cw, sns=sns, topic_arn="arn")
    m = compute_metrics(pairs(100, diverge=12), "SentimentOutput")
    pub.send_alerts(m, evaluate_alerts(m))
    sns.publish.assert_not_called()


def _api_fail(version):
    return RunResult("sentiment_classifier", version, "m", "txt", output=None, schema_valid=False,
                     error="API error: overloaded")


def test_api_outage_is_not_counted_as_drift():
    """Outages are not prompt regressions and must not trigger a rollback."""
    ps = pairs(90) + [(_api_fail("1.3"), sres("positive", 0.9, "1.2")) for _ in range(10)]
    m = compute_metrics(ps, "SentimentOutput")
    assert m.n == 90 and m.schema_compliance == 1.0 and m.divergence_rate == 0
    assert evaluate_alerts(m) == []


def test_contract_violation_still_counts_against_compliance():
    bad = RunResult("sentiment_classifier", "1.3", "m", "txt", output=None, schema_valid=False,
                    raw_text='{"sentiment_score": 1}', error="Contract violation: x")
    assert not bad.api_failed
    m = compute_metrics(pairs(90) + [(bad, sres("positive", 0.9, "1.2"))] * 10, "SentimentOutput")
    assert m.schema_compliance == pytest.approx(0.9)


def test_only_api_failures_is_rejected():
    with pytest.raises(ValueError):
        compute_metrics([(_api_fail("1.3"), _api_fail("1.2"))], "SentimentOutput")
