import pytest
import yaml

from src.config import ConfigError, load_router_config, parse_router_config, DEFAULT_ROUTER_CONFIG


def test_loads_article_defaults(config):
    assert config.tasks["sentiment"].default_model == "haiku"
    assert config.tasks["sentiment"].fallback_model == "sonnet"
    assert config.tasks["sentiment"].complexity_threshold == 0.6
    assert config.tasks["aspect_extraction"].default_model == "haiku_with_rag"
    assert config.tasks["aspect_extraction"].embedding_threshold == 0.8
    assert config.tasks["toxicity_detection"].fallback_model == "opus"
    assert config.breaker.failure_threshold == 5
    assert config.breaker.recovery_timeout_s == 60
    assert config.cache_ttl_seconds == 30 * 24 * 3600


def test_task_policies_match_article(config):
    t = config.tasks
    assert (t["sentiment"].timeout_s, t["sentiment"].retries, t["sentiment"].max_concurrency) == (1, 2, 1000)
    assert (t["aspect_extraction"].timeout_s, t["aspect_extraction"].retries, t["aspect_extraction"].max_concurrency) == (3, 1, 100)
    assert (t["toxicity_detection"].timeout_s, t["toxicity_detection"].retries, t["toxicity_detection"].max_concurrency) == (5, 1, 50)
    assert (t["aggregation"].timeout_s, t["aggregation"].retries) == (2, 3)


def test_haiku_matches_article1_model_id(config):
    assert config.model("haiku").model_id == "claude-haiku-4-5-20251001"


def test_rag_alias_inherits_base(config):
    rag = config.model("sonnet_with_rag")
    assert rag.rag and rag.model_id == config.model("sonnet").model_id


def test_env_override(monkeypatch):
    monkeypatch.setenv("OPUS_MODEL", "claude-opus-custom")
    assert load_router_config().model("opus").model_id == "claude-opus-custom"


def test_cost_math(config):
    # 120 in / 15 out on Haiku at $1 / $5 per Mtok
    assert config.model("haiku").cost(120, 15) == pytest.approx(0.000195)


def test_unknown_model_reference_rejected():
    raw = yaml.safe_load(open(DEFAULT_ROUTER_CONFIG))
    raw["tasks"]["sentiment"]["default_model"] = "gpt-9"
    with pytest.raises(ConfigError):
        parse_router_config(raw)


def test_unknown_pattern_model_rejected():
    raw = yaml.safe_load(open(DEFAULT_ROUTER_CONFIG))
    raw["patterns"]["cascade"]["models"] = ["haiku_with_rag", "nope"]
    with pytest.raises(ConfigError):
        parse_router_config(raw)
