"""
Airflow DAG for the two-layer healthcare data quality pipeline.

Schedule: Daily
Flow: ingest -> layer1 (100%) -> layer2 sample -> Claude Sonnet -> persist
      -> Snowflake (quality dim) -> dbt -> SLA checks / metrics

Retry safety: every task is keyed by the logical date (ds). Staging files are
overwritten by key, quarantine inserts are ON CONFLICT DO NOTHING, Layer 2 skips
already-flagged records, and Snowflake loads are MERGEs. A retried task never
double-quarantines or double-loads.

A second DAG (monthly) draws the 200-sample calibration set.
"""

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta
import json
import logging
import os

logger = logging.getLogger(__name__)

default_args = {
    "owner": "data-engineering",
    "depends_on_past": False,
    "email_on_failure": True,
    "email": ["data-team@company.com"],
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

SOURCE = "healthcare_claims"


def _staging_path(ds: str, name: str) -> str:
    base = os.environ.get("QUALITY_STAGING_DIR", "/tmp/quality_staging")
    d = os.path.join(base, ds)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.json")


def _write(ds, name, payload):
    path = _staging_path(ds, name)
    with open(path, "w") as f:   # overwrite-by-key: idempotent
        json.dump(payload, f, default=str)
    return path


def _read(ds, name):
    with open(_staging_path(ds, name)) as f:
        return json.load(f)


def _store():
    from src.quarantine import QuarantineStore
    return QuarantineStore(os.environ.get("QUARANTINE_DB_PATH", "/tmp/quarantine.db"))


def _ruleset():
    from src.rule_engine import load_ruleset
    return load_ruleset(os.environ.get("RULES_FILE", "rules/healthcare_claims_v1.yaml"))


def ingest_from_s3(**context):
    """Pull the day's claims and the patient reference set from S3."""
    from src.s3_io import load_claims_from_s3

    ds = context["ds"]
    bucket = os.environ["CLAIMS_S3_BUCKET"]
    claims = load_claims_from_s3(bucket, f"claims/{ds}/")
    patients = load_claims_from_s3(bucket, "reference/patients/")
    _write(ds, "claims", claims)
    _write(ds, "patients", [p["patient_id"] for p in patients])
    context["ti"].xcom_push(key="claim_count", value=len(claims))


def layer1_validate(**context):
    """Layer 1: deterministic rules on 100% of rows."""
    from src.pipeline import run_layer1

    ds = context["ds"]
    claims = _read(ds, "claims")
    patients = set(_read(ds, "patients"))
    store = _store()
    summary = run_layer1(
        claims, _ruleset(), store, run_id=f"{SOURCE}:{ds}",
        reference_data={"patients.patient_id": patients},
    )
    passed = summary.pop("passed_records")
    _write(ds, "layer1_passed", passed)
    context["ti"].xcom_push(key="layer1_summary", value=summary)
    if summary["duration_seconds"] > 600:
        logger.error("Layer 1 SLA breach: > 10 minutes")


def sample_for_layer2(**context):
    """Risk-based stratified sample of Layer 1 survivors."""
    from src.pipeline import select_layer2_sample
    from src.risk_sampler import RiskBasedSampler, SourceStats

    ds = context["ds"]
    passed = _read(ds, "layer1_passed")
    stats = SourceStats(
        source_system=SOURCE,
        error_rate_per_1000=float(os.environ.get("SOURCE_ERROR_RATE_PER_1000", "30")),
        business_criticality=5, consequence_severity=5,
    )
    tier, sample = select_layer2_sample(passed, _ruleset(), RiskBasedSampler(salt=ds), stats)
    _write(ds, "layer2_sample", {"tier": tier, "records": sample})
    context["ti"].xcom_push(key="sample_summary", value={"tier": tier, "sampled": len(sample)})


def semantic_validate(**context):
    """Layer 2: Claude Sonnet on the sample; flagged records go to quarantine."""
    from src.pipeline import run_layer2
    from src.risk_sampler import RiskBasedSampler, SourceStats
    from src.semantic_validator import SemanticValidator

    ds = context["ds"]
    ruleset = _ruleset()
    staged = _read(ds, "layer2_sample")
    passed = _read(ds, "layer1_passed")
    validator = SemanticValidator(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        examples=ruleset.layer_2_examples,
        domain=ruleset.domain,
        model=os.environ.get("SEMANTIC_MODEL", "claude-sonnet-5-5"),
        confidence_threshold=ruleset.layer_2.get("confidence_threshold"),
    )
    summary = run_layer2(
        passed, ruleset, validator, RiskBasedSampler(salt=ds), _store(),
        run_id=f"{SOURCE}:{ds}",
        source_stats=SourceStats(SOURCE, 0.0),
        sample=staged["records"], tier=staged["tier"],
    )
    production = summary.pop("production_records")
    unvalidated = summary.pop("unvalidated_records")
    _write(ds, "production_records", production)
    # Sampled records whose Claude call failed are held back, not promoted.
    _write(ds, "layer2_unvalidated", unvalidated)
    if unvalidated:
        logger.error(f"Layer 2: {len(unvalidated)} sampled records could not be validated; "
                     f"held out of production_records (see layer2_unvalidated.json)")
    context["ti"].xcom_push(key="layer2_summary", value=summary)


def persist_to_snowflake(**context):
    """MERGE quarantine rows (and any review decisions) into Snowflake QUALITY."""
    from src.quality_loader import QualityLoader

    rows = _store().all()
    if not rows:
        logger.info("Nothing quarantined. Skipping load.")
        return
    loader = QualityLoader(
        account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"], database=os.environ["SNOWFLAKE_DATABASE"],
        schema="QUALITY", warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
    )
    try:
        summary = loader.load_quarantine_records(rows)
    finally:
        loader.close()
    context["ti"].xcom_push(key="load_summary", value=summary)
    if summary["failed"]:
        # Fail the task so Airflow retries (MERGE is idempotent) instead of
        # silently leaving Snowflake's quality dimension incomplete.
        raise RuntimeError(f"Snowflake quality load: {summary['failed']} of {len(rows)} rows failed")


def check_review_sla(**context):
    """Alert if quarantined records have waited > 48h for human review."""
    from src.review_queue import ReviewQueue

    breaches = ReviewQueue(_store()).backlog_breaches()
    if breaches:
        logger.error(f"Human review SLA breached: {len(breaches)} records pending > 48h")
    context["ti"].xcom_push(key="review_sla_breaches", value=len(breaches))


def log_pipeline_metrics(**context):
    ti = context["ti"]
    l1 = ti.xcom_pull(key="layer1_summary", task_ids="layer1_validate") or {}
    l2 = ti.xcom_pull(key="layer2_summary", task_ids="semantic_validate") or {}
    logger.info("=" * 50)
    logger.info("DATA QUALITY SUMMARY")
    logger.info(f"  L1 total/passed/failed: {l1.get('total', 0)}/{l1.get('passed', 0)}/{l1.get('failed', 0)}")
    logger.info(f"  L2 sampled/flagged/errors: {l2.get('sampled', 0)}/{l2.get('flagged', 0)}/{l2.get('validation_errors', 0)}")
    logger.info(f"  L2 cost: ${(l2.get('cost') or {}).get('estimated_cost_usd', 0)}")
    logger.info("=" * 50)


def draw_monthly_calibration_sample(**context):
    """Pull 200 random L2-flagged records for two independent reviewers."""
    from src.calibration import draw_calibration_sample

    flagged = [r for r in _store().all() if r["flagged_by_layer"] == "L2"]
    sample = draw_calibration_sample(flagged, seed=int(context["ds"].replace("-", "")))
    _write(context["ds"], "calibration_sample", sample)
    context["ti"].xcom_push(key="calibration_sample_size", value=len(sample))


with DAG(
    dag_id="healthcare_data_quality_pipeline",
    default_args=default_args,
    description="Two-layer (syntactic + semantic AI) data quality for healthcare claims",
    schedule_interval="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["ai-pipeline", "data-quality", "healthcare"],
) as dag:

    ingest = PythonOperator(task_id="ingest", python_callable=ingest_from_s3)

    layer1 = PythonOperator(
        task_id="layer1_validate", python_callable=layer1_validate,
        execution_timeout=timedelta(minutes=10),   # Layer 1 SLA
    )

    sample = PythonOperator(task_id="sample_for_layer2", python_callable=sample_for_layer2)

    layer2 = PythonOperator(
        task_id="semantic_validate", python_callable=semantic_validate,
        execution_timeout=timedelta(hours=2),      # Layer 2 SLA
    )

    persist = PythonOperator(task_id="persist_to_snowflake", python_callable=persist_to_snowflake)

    run_dbt = BashOperator(
        task_id="run_dbt",
        bash_command="cd /opt/dbt && dbt run --select staging.stg_quarantine_records marts.dim_quality marts.fct_source_quality",
    )

    review_sla = PythonOperator(task_id="check_review_sla", python_callable=check_review_sla)

    metrics = PythonOperator(
        task_id="log_metrics", python_callable=log_pipeline_metrics, trigger_rule="all_done",
    )

    ingest >> layer1 >> sample >> layer2 >> persist >> run_dbt >> [review_sla, metrics]


with DAG(
    dag_id="healthcare_quality_monthly_calibration",
    default_args=default_args,
    description="Monthly 200-sample precision/kappa calibration set",
    schedule_interval="@monthly",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["data-quality", "calibration"],
) as calibration_dag:

    PythonOperator(task_id="draw_calibration_sample", python_callable=draw_monthly_calibration_sample)
