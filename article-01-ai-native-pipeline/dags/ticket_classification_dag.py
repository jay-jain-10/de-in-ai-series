"""
Airflow DAG for the AI-native ticket classification pipeline.

Schedule: Daily
Pipeline: S3 (ingest) → Claude Haiku (classify) → Snowflake (load) → dbt (transform)
"""

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta
import os
import logging

logger = logging.getLogger(__name__)

default_args = {
    "owner": "data-engineering",
    "depends_on_past": False,
    "email_on_failure": True,
    "email": ["data-team@company.com"],
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "sla": timedelta(hours=2),
}


def ingest_from_s3(**context):
    """Pull today's ticket batch from S3."""
    from src.batch_processor import load_tickets_from_s3

    execution_date = context["ds"]
    prefix = f"support-tickets/{execution_date}/"

    tickets = load_tickets_from_s3(
        bucket=os.environ["TICKETS_S3_BUCKET"],
        prefix=prefix,
    )

    context["ti"].xcom_push(key="tickets", value=tickets)
    context["ti"].xcom_push(key="ticket_count", value=len(tickets))
    logger.info(f"Ingested {len(tickets)} tickets for {execution_date}")


def classify_tickets(**context):
    """Run AI classification on ingested tickets."""
    from src.classifier import TicketClassifier
    from src.batch_processor import BatchProcessor

    tickets = context["ti"].xcom_pull(key="tickets", task_ids="ingest")
    if not tickets:
        logger.warning("No tickets to classify. Skipping.")
        return

    classifier = TicketClassifier(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.environ.get("CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001"),
    )
    processor = BatchProcessor(
        classifier,
        max_workers=int(os.environ.get("BATCH_MAX_WORKERS", "5")),
    )

    results = processor.process_tickets(tickets)

    cost = classifier.get_cost_estimate()
    logger.info(f"Classification cost: ${cost['estimated_cost_usd']}")

    context["ti"].xcom_push(key="classifications", value=results)
    context["ti"].xcom_push(key="api_cost", value=cost)


def load_to_snowflake(**context):
    """Load classified tickets into Snowflake."""
    from src.snowflake_loader import SnowflakeLoader

    results = context["ti"].xcom_pull(
        key="classifications", task_ids="classify"
    )
    if not results:
        logger.warning("No classifications to load. Skipping.")
        return

    loader = SnowflakeLoader(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema="RAW",
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
    )

    loader.initialize_table()
    summary = loader.load_classifications(results)
    context["ti"].xcom_push(key="load_summary", value=summary)


def log_pipeline_metrics(**context):
    """Log metrics for cost and quality observability."""
    ticket_count = context["ti"].xcom_pull(
        key="ticket_count", task_ids="ingest"
    ) or 0
    api_cost = context["ti"].xcom_pull(
        key="api_cost", task_ids="classify"
    ) or {}
    load_summary = context["ti"].xcom_pull(
        key="load_summary", task_ids="load"
    ) or {}

    logger.info("=" * 50)
    logger.info("PIPELINE SUMMARY")
    logger.info("=" * 50)
    logger.info(f"  Tickets ingested:  {ticket_count}")
    logger.info(f"  Records loaded:    {load_summary.get('loaded', 0)}")
    logger.info(f"  Records failed:    {load_summary.get('failed', 0)}")
    logger.info(f"  API cost:          ${api_cost.get('estimated_cost_usd', 0)}")
    logger.info(f"  Input tokens:      {api_cost.get('input_tokens', 0)}")
    logger.info(f"  Output tokens:     {api_cost.get('output_tokens', 0)}")
    logger.info(f"  Failure rate:      {api_cost.get('failure_rate', 0):.2%}")
    logger.info("=" * 50)


with DAG(
    dag_id="ticket_classification_pipeline",
    default_args=default_args,
    description="Classify support tickets using Claude Haiku",
    schedule_interval="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["ai-pipeline", "support-tickets", "classification"],
) as dag:

    ingest = PythonOperator(
        task_id="ingest",
        python_callable=ingest_from_s3,
    )

    classify = PythonOperator(
        task_id="classify",
        python_callable=classify_tickets,
        execution_timeout=timedelta(hours=1),
    )

    load = PythonOperator(
        task_id="load",
        python_callable=load_to_snowflake,
    )

    run_dbt = BashOperator(
        task_id="run_dbt",
        bash_command="cd /opt/dbt && dbt run --select staging.stg_ticket_classifications marts.fct_ticket_sentiment marts.fct_escalation_alerts",
    )

    metrics = PythonOperator(
        task_id="log_metrics",
        python_callable=log_pipeline_metrics,
        trigger_rule="all_done",
    )

    ingest >> classify >> load >> run_dbt >> metrics
