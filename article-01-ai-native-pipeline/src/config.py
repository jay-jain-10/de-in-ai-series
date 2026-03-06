"""Configuration management for the ticket classification pipeline."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001")
    max_retries: int = 3
    max_tokens: int = 300


@dataclass
class S3Config:
    bucket: str = os.getenv("TICKETS_S3_BUCKET", "")
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
class PipelineConfig:
    batch_max_workers: int = int(os.getenv("BATCH_MAX_WORKERS", "5"))
    confidence_threshold: float = 0.7
    anthropic: AnthropicConfig = None
    s3: S3Config = None
    snowflake: SnowflakeConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.s3 = self.s3 or S3Config()
        self.snowflake = self.snowflake or SnowflakeConfig()
