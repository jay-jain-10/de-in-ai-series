"""Production drift monitoring.

Every day ~1% of production requests are sampled and re-run through both the
current and the previous prompt version. From those pairs we compute:

  * output divergence      (warn > 10%, alert > 15%)
  * confidence degradation (alert if mean drops > 0.05) + histogram overlay
  * schema compliance      (target > 95%, alert < 90%)
  * hallucination rate     (alert > 5%, rollback if it rises > 3 pts)

Metrics are published to CloudWatch (custom metrics) and alerts go to SNS.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .config import GovernanceConfig
from .output_contracts import GROUNDED_FIELDS, find_ungrounded
from .prompt_runner import RunResult

logger = logging.getLogger(__name__)

HISTOGRAM_BINS = 10  # 0.0-0.1, ..., 0.9-1.0


def sample_requests(records: list, rate: float = 0.01, key=lambda r: r["input_id"], salt: str = "") -> list:
    """Deterministic hash-based sampling (stable across reruns / retries)."""
    out = []
    for r in records:
        h = hashlib.sha256(f"{salt}{key(r)}".encode()).hexdigest()
        if int(h[:8], 16) / 0xFFFFFFFF < rate:
            out.append(r)
    return out


def confidence_histogram(confidences: List[float], bins: int = HISTOGRAM_BINS) -> List[int]:
    counts = [0] * bins
    for c in confidences:
        counts[min(int(c * bins), bins - 1)] += 1
    return counts


@dataclass
class DriftMetrics:
    prompt_name: str
    current_version: str
    previous_version: str
    n: int
    divergence_rate: float
    avg_confidence_current: float
    avg_confidence_previous: float
    confidence_drop: float
    histogram_current: List[int]
    histogram_previous: List[int]
    schema_compliance: float
    hallucination_rate: float
    hallucination_rate_previous: float

    @property
    def hallucination_delta(self) -> float:
        return self.hallucination_rate - self.hallucination_rate_previous


@dataclass
class Alert:
    metric: str
    level: str  # "warn" | "alert"
    value: float
    threshold: float
    message: str
    triggers_rollback: bool = False


def _hallucinated(r: RunResult, contract: str) -> Optional[bool]:
    """None if the contract has no grounded fields or output invalid."""
    fields = GROUNDED_FIELDS.get(contract, [])
    if not fields or not r.schema_valid:
        return None
    return bool(find_ungrounded(r.output, r.input_text, fields))


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def compute_metrics(pairs: List[tuple], contract: str) -> DriftMetrics:
    """``pairs`` = [(current RunResult, previous RunResult), ...] on the same inputs."""
    if not pairs:
        raise ValueError("no sampled pairs")
    # An API outage says nothing about the prompt. Counting it as a schema
    # violation / divergence would trigger a rollback of a healthy version.
    pairs = [(c, p) for c, p in pairs if not c.api_failed and not p.api_failed]
    if not pairs:
        raise ValueError("no sampled pairs with model responses (API failures only)")
    cur = [p[0] for p in pairs]
    prev = [p[1] for p in pairs]

    diverged = sum(1 for c, p in pairs if c.comparable() != p.comparable())
    cur_conf = [r.confidence for r in cur if r.confidence is not None]
    prev_conf = [r.confidence for r in prev if r.confidence is not None]

    def hall_rate(rs):
        flags = [f for f in (_hallucinated(r, contract) for r in rs) if f is not None]
        return sum(flags) / len(flags) if flags else 0.0

    return DriftMetrics(
        prompt_name=cur[0].prompt_name,
        current_version=cur[0].prompt_version,
        previous_version=prev[0].prompt_version,
        n=len(pairs),
        divergence_rate=diverged / len(pairs),
        avg_confidence_current=_mean(cur_conf),
        avg_confidence_previous=_mean(prev_conf),
        confidence_drop=_mean(prev_conf) - _mean(cur_conf),
        histogram_current=confidence_histogram(cur_conf),
        histogram_previous=confidence_histogram(prev_conf),
        schema_compliance=sum(r.schema_valid for r in cur) / len(cur),
        hallucination_rate=hall_rate(cur),
        hallucination_rate_previous=hall_rate(prev),
    )


def evaluate_alerts(m: DriftMetrics, cfg: Optional[GovernanceConfig] = None) -> List[Alert]:
    cfg = cfg or GovernanceConfig()
    alerts: List[Alert] = []

    if m.divergence_rate > cfg.divergence_alert:
        alerts.append(Alert("divergence", "alert", m.divergence_rate, cfg.divergence_alert,
                            f"output divergence {m.divergence_rate:.1%} > {cfg.divergence_alert:.0%} (prompt changed significantly)",
                            triggers_rollback=True))
    elif m.divergence_rate > cfg.divergence_warn:
        alerts.append(Alert("divergence", "warn", m.divergence_rate, cfg.divergence_warn,
                            f"output divergence {m.divergence_rate:.1%} > {cfg.divergence_warn:.0%}: investigate"))

    if m.confidence_drop > cfg.confidence_alert_drop:
        alerts.append(Alert("confidence_drop", "alert", m.confidence_drop, cfg.confidence_alert_drop,
                            f"average confidence dropped {m.confidence_drop:.3f} points",
                            triggers_rollback=True))

    if m.schema_compliance < cfg.compliance_alert:
        alerts.append(Alert("schema_compliance", "alert", m.schema_compliance, cfg.compliance_alert,
                            f"schema compliance {m.schema_compliance:.1%} < {cfg.compliance_alert:.0%}",
                            triggers_rollback=True))
    elif m.schema_compliance < cfg.compliance_target:
        alerts.append(Alert("schema_compliance", "warn", m.schema_compliance, cfg.compliance_target,
                            f"schema compliance {m.schema_compliance:.1%} < {cfg.compliance_target:.0%} target"))

    if m.hallucination_rate > cfg.hallucination_alert:
        alerts.append(Alert("hallucination_rate", "alert", m.hallucination_rate, cfg.hallucination_alert,
                            f"hallucination rate {m.hallucination_rate:.1%} > {cfg.hallucination_alert:.0%}",
                            triggers_rollback=True))
    elif m.hallucination_delta > cfg.hallucination_rollback_delta:
        alerts.append(Alert("hallucination_rate", "alert", m.hallucination_delta, cfg.hallucination_rollback_delta,
                            f"hallucination rate rose {m.hallucination_delta:.1%} vs previous version",
                            triggers_rollback=True))
    return alerts


class MetricsPublisher:
    """Publishes drift metrics to CloudWatch and alerts to SNS (clients injected)."""

    NAMESPACE = "PromptGovernance"

    def __init__(self, cloudwatch=None, sns=None, topic_arn: str = ""):
        if cloudwatch is None or sns is None:
            import boto3

            cloudwatch = cloudwatch or boto3.client("cloudwatch")
            sns = sns or boto3.client("sns")
        self.cloudwatch = cloudwatch
        self.sns = sns
        self.topic_arn = topic_arn

    def publish_metrics(self, m: DriftMetrics) -> None:
        dims = [
            {"Name": "PromptName", "Value": m.prompt_name},
            {"Name": "PromptVersion", "Value": m.current_version},
        ]
        values = {
            "OutputDivergence": (m.divergence_rate, "Percent", 100),
            "ConfidenceDrop": (m.confidence_drop, "None", 1),
            "SchemaCompliance": (m.schema_compliance, "Percent", 100),
            "HallucinationRate": (m.hallucination_rate, "Percent", 100),
            "AvgConfidence": (m.avg_confidence_current, "None", 1),
        }
        data = [
            {"MetricName": k, "Dimensions": dims, "Value": v * scale, "Unit": unit}
            for k, (v, unit, scale) in values.items()
        ]
        self.cloudwatch.put_metric_data(Namespace=self.NAMESPACE, MetricData=data)

    def send_alerts(self, m: DriftMetrics, alerts: List[Alert]) -> None:
        alerts = [a for a in alerts if a.level == "alert"]
        if not alerts or not self.topic_arn:
            return
        body = "\n".join(f"- {a.message}" for a in alerts)
        self.sns.publish(
            TopicArn=self.topic_arn,
            Subject=f"[prompt-drift] {m.prompt_name} v{m.current_version}",
            Message=body,
        )
