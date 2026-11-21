"""Configuration management for the multi-model orchestration pipeline.

Follows Article 1 conventions (dataclasses + env vars + python-dotenv) and adds a
YAML-driven router config so routing decisions are configuration, not code.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

DEFAULT_ROUTER_CONFIG = Path(__file__).resolve().parent.parent / "config" / "router.yaml"

# Env overrides for model ids; HAIKU falls back to Article 1's CLASSIFICATION_MODEL.
_MODEL_ENV = {
    "haiku": ("HAIKU_MODEL", "CLASSIFICATION_MODEL"),
    "sonnet": ("SONNET_MODEL",),
    "opus": ("OPUS_MODEL",),
}


class ConfigError(ValueError):
    """Raised when the router YAML is inconsistent."""


@dataclass
class ModelSpec:
    alias: str
    model_id: str
    input_per_mtok: float
    output_per_mtok: float
    rag: bool = False
    base_alias: str = ""

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


@dataclass
class TaskRoute:
    name: str
    router: str = ""
    default_model: str = ""
    fallback_model: str = ""
    complexity_threshold: float = 0.6
    embedding_threshold: float = 0.8
    safety_keywords: list = field(default_factory=list)
    timeout_s: float = 5.0
    retries: int = 1
    max_concurrency: int = 10


@dataclass
class BreakerSettings:
    failure_threshold: int = 5
    recovery_timeout_s: float = 60.0
    ramp_steps: tuple = (0.01, 0.10, 0.50, 1.0)
    retry_after_s: int = 3600
    fallback_rate_alert: float = 0.05


@dataclass
class RouterConfig:
    models: dict
    tasks: dict
    patterns: dict
    breaker: BreakerSettings
    cache_ttl_seconds: int = 30 * 24 * 3600
    cache_min_hit_rate: float = 0.05
    human_review_confidence: float = 0.7

    def model(self, alias: str) -> ModelSpec:
        try:
            return self.models[alias]
        except KeyError:
            raise ConfigError(f"Unknown model alias: {alias}")


def load_router_config(path: Optional[str] = None) -> RouterConfig:
    path = Path(path or os.getenv("ROUTER_CONFIG_PATH") or DEFAULT_ROUTER_CONFIG)
    with open(path) as f:
        raw = yaml.safe_load(f)
    return parse_router_config(raw)


def parse_router_config(raw: dict) -> RouterConfig:
    models: dict = {}
    for alias, spec in raw["models"].items():
        if "base" in spec:
            continue
        model_id = spec["model_id"]
        for env_name in _MODEL_ENV.get(alias, ()):
            if os.getenv(env_name):
                model_id = os.environ[env_name]
                break
        models[alias] = ModelSpec(
            alias, model_id, float(spec["input_per_mtok"]),
            float(spec["output_per_mtok"]), base_alias=alias,
        )
    for alias, spec in raw["models"].items():
        if "base" in spec:
            base = models.get(spec["base"])
            if base is None:
                raise ConfigError(f"{alias}: unknown base model {spec['base']}")
            models[alias] = ModelSpec(
                alias, base.model_id, base.input_per_mtok, base.output_per_mtok,
                rag=bool(spec.get("rag", False)), base_alias=base.alias,
            )

    tasks = {}
    for name, spec in raw["tasks"].items():
        tasks[name] = TaskRoute(name=name, **spec)
        for key in ("default_model", "fallback_model"):
            ref = getattr(tasks[name], key)
            if ref and ref not in models:
                raise ConfigError(f"task {name}: {key} '{ref}' is not a defined model")

    patterns = raw.get("patterns", {})
    for pname, pspec in patterns.items():
        for key, val in pspec.items():
            vals = val if isinstance(val, list) else [val]
            if key.endswith("model") or key.endswith("models") or key in tasks:
                for v in vals:
                    if isinstance(v, str) and v not in models:
                        raise ConfigError(f"pattern {pname}: '{v}' is not a defined model")

    cb = raw.get("circuit_breaker", {})
    breaker = BreakerSettings(
        failure_threshold=cb.get("failure_threshold", 5),
        recovery_timeout_s=cb.get("recovery_timeout_s", 60),
        ramp_steps=tuple(cb.get("ramp_steps", (0.01, 0.10, 0.50, 1.0))),
        retry_after_s=cb.get("retry_after_s", 3600),
        fallback_rate_alert=cb.get("fallback_rate_alert", 0.05),
    )
    cache = raw.get("cache", {})
    return RouterConfig(
        models=models, tasks=tasks, patterns=patterns, breaker=breaker,
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", cache.get("ttl_seconds", 2592000))),
        cache_min_hit_rate=cache.get("min_hit_rate", 0.05),
        human_review_confidence=raw.get("human_review_confidence", 0.7),
    )


@dataclass
class AnthropicConfig:
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    max_retries: int = 3
    max_tokens: int = 300


@dataclass
class RedisConfig:
    url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")


@dataclass
class PipelineConfig:
    batch_max_workers: int = int(os.getenv("BATCH_MAX_WORKERS", "16"))
    anthropic: AnthropicConfig = None
    redis: RedisConfig = None
    router: RouterConfig = None

    def __post_init__(self):
        self.anthropic = self.anthropic or AnthropicConfig()
        self.redis = self.redis or RedisConfig()
        self.router = self.router or load_router_config()
