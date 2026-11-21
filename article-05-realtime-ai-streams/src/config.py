"""Configuration for the hybrid streaming + AI fraud pipeline.

Defaults mirror the numbers in Article 5.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("ENRICHMENT_MODEL", "claude-haiku-4-5-20251001")
    max_retries: int = 3
    max_tokens: int = 1500
    # Same Haiku pricing convention as Article 1 (USD per 1M tokens).
    input_price_per_mtok: float = 1.00
    output_price_per_mtok: float = 5.00


@dataclass
class KafkaConfig:
    bootstrap_servers: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    group_id: str = os.getenv("KAFKA_GROUP_ID", "fraud-detector")
    events_topic: str = os.getenv("KAFKA_EVENTS_TOPIC", "transactions")
    scores_topic: str = os.getenv("KAFKA_SCORES_TOPIC", "risk-scores")
    alerts_topic: str = os.getenv("KAFKA_ALERTS_TOPIC", "fraud-alerts")
    dlq_topic: str = "transactions-dlq"


@dataclass
class RedisConfig:
    url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    timeout_ms: int = int(os.getenv("REDIS_TIMEOUT_MS", "100"))  # article: 100ms bound


@dataclass
class BatchConfig:
    max_events: int = 10          # "every 10 events ..."
    max_wait_seconds: float = 2.0  # "... or every 2 seconds"
    wide_max_events: int = 50     # under backpressure
    wide_max_wait_seconds: float = 5.0


@dataclass
class BackpressureConfig:
    lag_events_high: int = 5000     # widen batches
    lag_events_critical: int = 10000  # also sample 50% (article: "even higher")
    lag_seconds_high: float = 5.0   # staleness = data quality event
    critical_sample_rate: float = 0.5


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    open_seconds: float = 60.0


@dataclass
class CostConfig:
    daily_budget_usd: float = float(os.getenv("DAILY_BUDGET_USD", "50"))
    # (spend / budget ratio at or above, sample rate) - evaluated highest first
    tiers: tuple = ((2.0, 0.1), (1.5, 0.25), (1.0, 0.5))


@dataclass
class SLAConfig:
    critical_p99_ms: float = 50.0
    enrichment_seconds: float = 5.0
    enrichment_fraction: float = 0.95
    alert_seconds: float = 10.0


@dataclass
class PipelineConfig:
    alert_score_threshold: int = 80
    enrichment_version: int = int(os.getenv("ENRICHMENT_VERSION", "1"))
    postgres_dsn: str = os.getenv("POSTGRES_DSN", "")
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    kafka: KafkaConfig = field(default_factory=KafkaConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    backpressure: BackpressureConfig = field(default_factory=BackpressureConfig)
    breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    sla: SLAConfig = field(default_factory=SLAConfig)
