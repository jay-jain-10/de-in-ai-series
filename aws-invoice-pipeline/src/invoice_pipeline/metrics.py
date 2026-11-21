"""CloudWatch custom metrics + alarm (Part 3)."""
import boto3
from datetime import datetime

cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")


def publish_pipeline_metrics(
    invoice_id: str,
    stage: str,
    duration_ms: float,
    confidence: float,
    status: str,
    cost_usd: float,
):
    """
    Publish custom metrics to CloudWatch after each pipeline stage.
    """
    timestamp = datetime.utcnow()

    cloudwatch.put_metric_data(
        Namespace="InvoicePipeline",
        MetricData=[
            # How long each stage takes
            {
                "MetricName": "StageDuration",
                "Dimensions": [{"Name": "Stage", "Value": stage}],
                "Value": duration_ms,
                "Unit": "Milliseconds",
                "Timestamp": timestamp,
            },
            # Confidence distribution — are we getting dumber?
            {
                "MetricName": "ExtractionConfidence",
                "Value": confidence,
                "Unit": "None",
                "Timestamp": timestamp,
            },
            # Cost per invoice — catches budget blowouts early
            {
                "MetricName": "CostPerInvoice",
                "Value": cost_usd,
                "Unit": "None",
                "Timestamp": timestamp,
            },
            # Pass/fail rate
            {
                "MetricName": "ValidationResult",
                "Dimensions": [{"Name": "Status", "Value": status}],
                "Value": 1,
                "Unit": "Count",
                "Timestamp": timestamp,
            },
        ],
    )


# Alarm: fires when >20% of daily volume is flagged for review
def create_high_review_rate_alarm():
    cloudwatch.put_metric_alarm(
        AlarmName="HighReviewRate",
        Namespace="InvoicePipeline",
        MetricName="ValidationResult",
        Dimensions=[{"Name": "Status", "Value": "flagged_for_review"}],
        Statistic="Sum",
        Period=86400,  # Daily
        EvaluationPeriods=1,
        Threshold=800,  # 20% of 4000
        ComparisonOperator="GreaterThanThreshold",
        AlarmActions=["arn:aws:sns:us-east-1:123456789:ops-alerts"],
    )
