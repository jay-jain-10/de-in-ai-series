"""Configuration management for the prompt governance pipeline.

Follows the Article 1 conventions (dataclasses + env vars). All governance
thresholds below are the numbers quoted in Article 3.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Same Haiku id as Article 1
    model: str = os.getenv("CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001")
    escalation_model: str = os.getenv("ESCALATION_MODEL", "claude-sonnet-5-5")
    max_retries: int = 3
    max_tokens: int = 300


@dataclass
class RegistryConfig:
    bucket: str = os.getenv("PROMPT_REGISTRY_BUCKET", "")
    key: str = os.getenv("PROMPT_REGISTRY_KEY", "prompts/registry.yaml")
    region: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    sns_topic_arn: str = os.getenv("PROMPT_ALERT_SNS_TOPIC_ARN", "")


@dataclass
class SnowflakeConfig:
    account: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
    user: str = os.getenv("SNOWFLAKE_USER", "")
    password: str = os.getenv("SNOWFLAKE_PASSWORD", "")
    database: str = os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    schema: str = "RAW"
    warehouse: str = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")


@dataclass
class GovernanceConfig:
    """Thresholds from the article."""

    # CI gate: block PR if > 5% of golden cases fail, or avg confidence
    # drops by more than 0.05 points versus the baseline version.
    max_golden_fail_rate: float = float(os.getenv("GOLDEN_MAX_FAIL_RATE", "0.05"))
    max_confidence_drop: float = float(os.getenv("GOLDEN_MAX_CONFIDENCE_DROP", "0.05"))
    improvement_green_flag: float = 0.05

    # Production monitoring
    sample_rate: float = float(os.getenv("MONITOR_SAMPLE_RATE", "0.01"))
    divergence_warn: float = 0.10          # investigate
    divergence_alert: float = 0.15         # alert
    compliance_target: float = 0.95        # should be above
    compliance_alert: float = 0.90         # alert / rollback below
    hallucination_alert: float = 0.05      # alert / rollback above
    hallucination_rollback_delta: float = 0.03  # rollback if rate rises > 3 pts
    confidence_alert_drop: float = 0.05    # alert / rollback if mean drops > 0.05
    monitoring_window_days: int = 7        # auto-rollback window after deploy
    rollback_sla_seconds: int = 5 * 60     # rollback in < 5 minutes


@dataclass
class PipelineConfig:
    anthropic: AnthropicConfig = None
    registry: RegistryConfig = None
    snowflake: SnowflakeConfig = None
    governance: GovernanceConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.registry = self.registry or RegistryConfig()
        self.snowflake = self.snowflake or SnowflakeConfig()
        self.governance = self.governance or GovernanceConfig()
