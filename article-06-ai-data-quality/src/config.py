"""Configuration management for the two-layer data quality pipeline."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# Alert (flag) thresholds on the Layer 2 anomaly confidence, by domain criticality.
# Article 6, "Calibration and Trust".
CONFIDENCE_THRESHOLDS = {
    "healthcare": 0.60,   # safety-critical: accept false positives, catch errors
    "financial": 0.70,    # fraud related
    "marketing": 0.80,    # informational: avoid alert fatigue
}

# Risk-based sampling rates per tier. Article 6, "The Sampling Strategy".
SAMPLING_RATES = {"high": 1.00, "medium": 0.20, "low": 0.03}

# Risk score boundaries: >80 high, 50-80 medium, <50 low.
HIGH_RISK_MIN_SCORE = 80
MEDIUM_RISK_MIN_SCORE = 50

# Calibration protocol. Article 6, "Calibration Protocol".
CALIBRATION_SAMPLE_SIZE = 200
KAPPA_MIN = 0.7
PRECISION_MIN = 0.80

# SLAs
LAYER1_SLA_MINUTES = 10
LAYER2_SLA_HOURS = 2
HUMAN_REVIEW_SLA_HOURS = 48


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("SEMANTIC_MODEL", "claude-sonnet-5-5")
    max_retries: int = 3
    max_tokens: int = 400


@dataclass
class S3Config:
    bucket: str = os.getenv("CLAIMS_S3_BUCKET", "")
    region: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


@dataclass
class SnowflakeConfig:
    account: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
    user: str = os.getenv("SNOWFLAKE_USER", "")
    password: str = os.getenv("SNOWFLAKE_PASSWORD", "")
    database: str = os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    schema: str = "QUALITY"
    warehouse: str = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")


@dataclass
class PipelineConfig:
    rules_file: str = os.getenv("RULES_FILE", "rules/healthcare_claims_v1.yaml")
    quarantine_db_path: str = os.getenv("QUARANTINE_DB_PATH", "/tmp/quarantine.db")
    staging_dir: str = os.getenv("QUALITY_STAGING_DIR", "/tmp/quality_staging")
    batch_max_workers: int = int(os.getenv("BATCH_MAX_WORKERS", "5"))
    review_sample_rate: float = float(os.getenv("REVIEW_SAMPLE_RATE", "0.10"))
    anthropic: AnthropicConfig = None
    s3: S3Config = None
    snowflake: SnowflakeConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.s3 = self.s3 or S3Config()
        self.snowflake = self.snowflake or SnowflakeConfig()
