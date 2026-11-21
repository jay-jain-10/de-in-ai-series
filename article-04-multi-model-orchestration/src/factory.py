"""Convenience wiring for local runs / notebooks (production would inject real clients)."""

import logging

from .cache import InMemoryCache, RedisCache, ReviewCache
from .config import PipelineConfig
from .gateway import ModelGateway
from .patterns import Orchestrator
from .router import ModelRouter

logger = logging.getLogger(__name__)


def build_orchestrator(client=None, cache_backend=None, config=None) -> Orchestrator:
    """Build an Orchestrator. With no arguments uses ANTHROPIC_API_KEY and REDIS_URL."""
    pipeline = PipelineConfig(router=config) if config else PipelineConfig()
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=pipeline.anthropic.api_key)
    if cache_backend is None:
        try:
            cache_backend = RedisCache.from_url(pipeline.redis.url)
            cache_backend._client.ping()  # from_url is lazy: verify Redis is reachable
        except Exception as e:
            logger.warning("Redis unavailable (%s); using in-memory cache", e)
            cache_backend = InMemoryCache()
    rc = pipeline.router
    cache = ReviewCache(cache_backend, rc.cache_ttl_seconds, rc.cache_min_hit_rate)
    gateway = ModelGateway(client, rc, cache=cache)
    return Orchestrator(gateway, ModelRouter(rc))
