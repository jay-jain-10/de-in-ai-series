"""CloudWatch-style metric emitter (client injected).

Aggregates cost events and publishes: daily_cost, cost_per_model,
cost_per_pipeline, cache_hit_rate, token_waste_rate (output tokens of failed
calls / all output tokens). Also defines the article's alarms.

Each flush() publishes the spend observed since the previous flush and then
resets, so cost metrics are increments. Alarms therefore use Statistic="Sum"
over a 1-day period to get the daily total (Maximum would compare the single
largest invocation against the daily budget and practically never fire).
"""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Sequence

from src.cost_events import CostEvent


class MetricEmitter:
    def __init__(self, client=None, namespace: str = "AIFinOps", region: str = "us-east-1",
                 now=lambda: datetime.now(timezone.utc)):
        if client is None:
            import boto3
            client = boto3.client("cloudwatch", region_name=region)
        self.client, self.namespace, self._now = client, namespace, now
        self.reset()

    def reset(self) -> None:
        self.daily_cost = 0.0
        self.cost_by_model: Dict[str, float] = defaultdict(float)
        self.cost_by_pipeline: Dict[str, float] = defaultdict(float)
        self.calls = 0
        self.cache_hits = 0
        self.output_tokens = 0
        self.wasted_output_tokens = 0

    def observe(self, e: CostEvent) -> None:
        self.calls += 1
        self.daily_cost += e.cost_usd
        self.cost_by_model[e.model] += e.cost_usd
        self.cost_by_pipeline[e.pipeline_name] += e.cost_usd
        self.cache_hits += int(e.cache_hit)
        self.output_tokens += e.output_tokens
        if not e.success:
            self.wasted_output_tokens += e.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.calls if self.calls else 0.0

    @property
    def token_waste_rate(self) -> float:
        return self.wasted_output_tokens / self.output_tokens if self.output_tokens else 0.0

    def build_metric_data(self) -> List[dict]:
        ts = self._now()
        def m(name, value, unit, dims=None):
            d = {"MetricName": name, "Value": float(value), "Unit": unit, "Timestamp": ts}
            if dims:
                d["Dimensions"] = [{"Name": k, "Value": v} for k, v in dims.items()]
            return d
        data = [
            m("daily_cost", self.daily_cost, "None"),
            m("cache_hit_rate", self.cache_hit_rate, "None"),
            m("token_waste_rate", self.token_waste_rate, "None"),
        ]
        data += [m("cost_per_model", v, "None", {"Model": k}) for k, v in self.cost_by_model.items()]
        data += [m("cost_per_pipeline", v, "None", {"Pipeline": k}) for k, v in self.cost_by_pipeline.items()]
        return data

    def flush(self) -> int:
        """Publish in chunks of 20 (PutMetricData limit). Returns metrics sent."""
        data = self.build_metric_data()
        for i in range(0, len(data), 20):
            self.client.put_metric_data(Namespace=self.namespace, MetricData=data[i:i + 20])
        # Reset so a reused (warm-Lambda) emitter publishes increments, not
        # cumulative totals that a Sum alarm would double count.
        self.reset()
        return len(data)


def alarm_definitions(namespace: str = "AIFinOps", daily_budget: float = 200.0,
                      expected_cache_hit_rate: float = 0.25, max_waste_rate: float = 0.05,
                      pipeline_budget: float = 200.0,
                      pipelines: Sequence[str] = ()) -> List[dict]:
    """The article's alarms as put_metric_alarm kwargs.

    cost_per_pipeline is published with a Pipeline dimension, and a CloudWatch
    alarm only matches a metric with the exact same dimensions, so the
    per-pipeline budget alarm is created once per name in `pipelines`.
    """
    base = dict(Namespace=namespace, Period=86400, EvaluationPeriods=1, TreatMissingData="notBreaching")
    defs = [
        dict(base, AlarmName="ai-daily-cost-over-budget", MetricName="daily_cost",
             Statistic="Sum", Threshold=daily_budget, ComparisonOperator="GreaterThanThreshold"),
        dict(base, AlarmName="ai-cache-hit-rate-low", MetricName="cache_hit_rate",
             Statistic="Average", Threshold=expected_cache_hit_rate, ComparisonOperator="LessThanThreshold"),
        dict(base, AlarmName="ai-token-waste-high", MetricName="token_waste_rate",
             Statistic="Average", Threshold=max_waste_rate, ComparisonOperator="GreaterThanThreshold"),
    ]
    defs += [
        dict(base, AlarmName=f"ai-pipeline-over-budget-{p}", MetricName="cost_per_pipeline",
             Dimensions=[{"Name": "Pipeline", "Value": p}],
             Statistic="Sum", Threshold=pipeline_budget, ComparisonOperator="GreaterThanThreshold")
        for p in pipelines
    ]
    # "Daily cost > 2x rolling 7-day average" is evaluated by check_variance()
    # in cost_sla.py (CloudWatch anomaly-detection alarms are a drop-in alternative).
    return defs


def create_alarms(client, **kw) -> int:
    defs = alarm_definitions(**kw)
    for d in defs:
        client.put_metric_alarm(**d)
    return len(defs)
