"""Configuration for the cost-engineering (FinOps) pipeline."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    haiku_model: str = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")
    sonnet_model: str = os.getenv("SONNET_MODEL", "claude-sonnet-5-5")
    opus_model: str = os.getenv("OPUS_MODEL", "claude-opus-5-5")
    max_retries: int = 3
    max_tokens: int = 300


@dataclass
class AwsConfig:
    region: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    cache_bucket: str = os.getenv("COST_CACHE_S3_BUCKET", "")
    firehose_stream: str = os.getenv("COST_FIREHOSE_STREAM", "ai-cost-events")
    cloudwatch_namespace: str = os.getenv("CLOUDWATCH_NAMESPACE", "AIFinOps")


@dataclass
class SnowflakeConfig:
    account: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
    user: str = os.getenv("SNOWFLAKE_USER", "")
    password: str = os.getenv("SNOWFLAKE_PASSWORD", "")
    database: str = os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    schema: str = "RAW"
    warehouse: str = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")


@dataclass
class CostSLAConfig:
    """Cost SLAs from the article."""
    max_cost_per_record_usd: float = float(os.getenv("MAX_COST_PER_RECORD_USD", "0.005"))
    max_daily_cost_usd: float = float(os.getenv("MAX_DAILY_COST_USD", "200"))
    variance_threshold: float = float(os.getenv("COST_VARIANCE_THRESHOLD", "2.0"))


@dataclass
class LeverConfig:
    cache_ttl_days: int = 30                 # article: TTL-based expiry after 30 days
    semantic_similarity_threshold: float = 0.95
    haiku_max_score: float = 3.0             # score < 3  -> Haiku
    sonnet_max_score: float = 7.0            # score < 7  -> Sonnet, else Opus
    batch_discount: float = 0.50
    batch_min_size: int = 100                # article: accumulate 100-1000 requests
    batch_max_size: int = 1000


@dataclass
class FinOpsConfig:
    anthropic: AnthropicConfig = None
    aws: AwsConfig = None
    snowflake: SnowflakeConfig = None
    sla: CostSLAConfig = None
    levers: LeverConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.aws = self.aws or AwsConfig()
        self.snowflake = self.snowflake or SnowflakeConfig()
        self.sla = self.sla or CostSLAConfig()
        self.levers = self.levers or LeverConfig()
