"""Configuration management for the AI data platform (capstone)."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-5-5"


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    extraction_model: str = os.getenv("EXTRACTION_MODEL", SONNET_MODEL)  # needs reasoning
    classification_model: str = os.getenv("CLASSIFICATION_MODEL", HAIKU_MODEL)  # simple pattern matching
    max_retries: int = 3
    max_tokens: int = 1024


def _default_pricing() -> dict:
    """USD per 1M tokens (input, output). Same Haiku pricing as Article 1."""
    return {
        HAIKU_MODEL: {"input": 1.00, "output": 5.00},
        SONNET_MODEL: {"input": 3.00, "output": 15.00},
    }


@dataclass
class GatewayConfig:
    l1_ttl_seconds: int = 24 * 3600            # Redis hot cache: 1 day
    l2_ttl_seconds: int = 30 * 24 * 3600       # S3 warm cache: 30 days
    breaker_error_threshold: float = 0.10      # open when error rate > 10%
    breaker_window: int = 20
    breaker_min_calls: int = 10
    breaker_cooldown_seconds: float = 30.0
    default_daily_quota: int = 5000            # claims team: 5000 calls/day
    pricing: dict = field(default_factory=_default_pricing)
    teams_file: str = os.getenv("TEAMS_FILE", "config/teams.yaml")


@dataclass
class ProcessorConfig:
    chunk_threshold_tokens: int = 5000   # documents above this are chunked
    max_chunk_tokens: int = 3500         # chunk size
    overlap_tokens: int = 200
    hard_chunk_limit_tokens: int = 4000  # data contract: chunks must be < 4000 tokens
    partial_extraction_confidence: float = 0.6


@dataclass
class ValidationConfig:
    layer2_sample_rate: float = float(os.getenv("LAYER2_SAMPLE_RATE", "0.1"))
    confidence_threshold: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))
    accuracy_alert_threshold: float = 0.95   # monthly accuracy < 95% -> adjust prompts


@dataclass
class KafkaConfig:
    bootstrap_servers: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    incoming_topic: str = os.getenv("KAFKA_INCOMING_TOPIC", "documents.incoming")
    cost_topic: str = os.getenv("KAFKA_COST_TOPIC", "gateway.cost_events")


@dataclass
class CacheConfig:
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    s3_bucket: str = os.getenv("CACHE_S3_BUCKET", "")
    region: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


@dataclass
class SnowflakeConfig:
    account: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
    user: str = os.getenv("SNOWFLAKE_USER", "")
    password: str = os.getenv("SNOWFLAKE_PASSWORD", "")
    database: str = os.getenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    schema: str = "RAW"
    warehouse: str = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")


@dataclass
class SLAConfig:
    ingest_to_warehouse_seconds: float = 4 * 3600   # 95% within 4 hours
    ingest_to_warehouse_percentile: float = 0.95
    gateway_availability: float = 0.995             # 99.5%
    cost_lag_seconds: float = 3600                  # cost events within 1 hour
    review_wait_seconds: float = 48 * 3600          # exceptions reviewed within 48h


@dataclass
class PlatformConfig:
    processing_version: str = os.getenv("PROCESSING_VERSION", "2026.11.0")
    anthropic: AnthropicConfig = None
    gateway: GatewayConfig = None
    processor: ProcessorConfig = None
    validation: ValidationConfig = None
    kafka: KafkaConfig = None
    cache: CacheConfig = None
    snowflake: SnowflakeConfig = None
    sla: SLAConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.gateway = self.gateway or GatewayConfig()
        self.processor = self.processor or ProcessorConfig()
        self.validation = self.validation or ValidationConfig()
        self.kafka = self.kafka or KafkaConfig()
        self.cache = self.cache or CacheConfig()
        self.snowflake = self.snowflake or SnowflakeConfig()
        self.sla = self.sla or SLAConfig()
