"""Configuration management for the contract extraction pipeline."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# Clause types extracted by the pipeline. governing_law was added in
# extraction_version 2.0 (schema evolution example from the article).
CLAUSE_TYPES_V1 = ("payment_terms", "termination_conditions", "liability_cap")
CLAUSE_TYPES_V2 = CLAUSE_TYPES_V1 + ("governing_law",)


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Haiku: cheap segmentation. Sonnet: extraction.
    segmentation_model: str = os.getenv("SEGMENTATION_MODEL", "claude-haiku-4-5-20251001")
    extraction_model: str = os.getenv("EXTRACTION_MODEL", "claude-sonnet-5-5")
    max_retries: int = 3
    segmentation_max_tokens: int = 1500
    extraction_max_tokens: int = 600


@dataclass
class S3Config:
    bucket: str = os.getenv("CONTRACTS_S3_BUCKET", "")
    region: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    quarantine_prefix: str = "quarantine/"


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
    extraction_version: float = float(os.getenv("EXTRACTION_VERSION", "2.1"))
    prompt_version: str = os.getenv("PROMPT_VERSION", "claude-extraction-v3")

    # Format detection
    min_chars_per_digital_page: int = 50

    # OCR
    textract_cost_per_1000_pages: float = 1.50
    ocr_min_confidence: float = 0.5      # all pages below this -> DLQ (stage "ocr")
    min_page_chars_after_ocr: int = 5    # noise filter for blank / signature pages

    # Chunking
    max_chunk_tokens: int = 1000         # fallback splitter + oversize guard
    max_segmentation_chars: int = 60_000

    # Extraction / validation
    max_extraction_attempts: int = 3
    confidence_threshold: float = 0.7    # below -> human review queue (24h SLA)
    high_confidence: float = 0.95
    medium_confidence: float = 0.75
    low_confidence: float = 0.5
    payment_terms_min_days: int = 15
    payment_terms_max_days: int = 180
    median_confidence_floor: float = 0.75

    # Orchestration
    fargate_page_threshold: int = 30     # larger docs go to Fargate

    anthropic: AnthropicConfig = None
    s3: S3Config = None
    snowflake: SnowflakeConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.s3 = self.s3 or S3Config()
        self.snowflake = self.snowflake or SnowflakeConfig()

    @property
    def clause_types(self):
        return CLAUSE_TYPES_V2 if self.extraction_version >= 2.0 else CLAUSE_TYPES_V1
