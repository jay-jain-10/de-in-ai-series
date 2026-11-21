"""FastAPI AI Gateway: single entry point for all LLM calls on the platform.

Endpoints: POST /v1/{extract,classify,summarize,validate,score}, GET /v1/usage,
GET /v1/cost-report, GET /health, GET /metrics. Auth via X-API-Key.
"""

import logging
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from ..config import PlatformConfig
from ..contracts import (ClassifyRequest, ExtractRequest, ScoreRequest, SummarizeRequest,
                         ValidateRequest)
from ..messaging import Publisher
from ..observability import MetricsRegistry
from .cache import InMemoryCache, TieredCache
from .circuit_breaker import CircuitBreaker
from .cost import CostTracker
from .llm import LLMClient
from .quota import AuthRegistry, QuotaExceeded, QuotaManager, TeamAccount
from .router import ModelCatalog, ModelRouter
from .service import GatewayService

log = logging.getLogger(__name__)

def build_service(config: PlatformConfig, llm: LLMClient, accounts: list[TeamAccount],
                  cache: Optional[TieredCache] = None, publisher: Optional[Publisher] = None,
                  metrics: Optional[MetricsRegistry] = None, breaker: Optional[CircuitBreaker] = None,
                  quota: Optional[QuotaManager] = None, sleep=None, clock=None):
    """Wire the gateway from injectable parts (cache, LLM, Kafka publisher)."""
    gw = config.gateway
    cache = cache or TieredCache(InMemoryCache(), InMemoryCache(), gw.l1_ttl_seconds, gw.l2_ttl_seconds)
    kwargs = {"clock": clock} if clock else {}
    cost = CostTracker(gw.pricing, publisher, config.kafka.cost_topic, **kwargs)
    router = ModelRouter(ModelCatalog(config.anthropic, gw.pricing))
    svc_kwargs = {"sleep": sleep} if sleep else {}
    service = GatewayService(config, llm, cache, cost, router, quota or QuotaManager(),
                             breaker=breaker, metrics=metrics, **svc_kwargs)
    return service, AuthRegistry(accounts)


def create_app(service: GatewayService, auth: AuthRegistry) -> FastAPI:
    app = FastAPI(title="AI Gateway", version="1.0")
    app.state.service, app.state.auth = service, auth

    def current_team(x_api_key: Optional[str] = Header(default=None)) -> TeamAccount:
        account = auth.authenticate(x_api_key)
        if account is None:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return account

    def check_caller(req, account: TeamAccount):
        if req.caller.team != account.team:
            raise HTTPException(status_code=403,
                                detail=f"API key belongs to team '{account.team}', "
                                       f"not '{req.caller.team}'")

    @app.exception_handler(QuotaExceeded)
    async def quota_handler(_, exc: QuotaExceeded):
        service.metrics.inc("gateway_quota_rejections_total", team=exc.team)
        return JSONResponse(status_code=429, headers={"Retry-After": "3600"},
                            content={"error": "quota_exceeded", "team": exc.team,
                                     "daily_limit": exc.limit, "message": str(exc)})

    def endpoint(path: str, model_cls, method_name: str):
        # Sync on purpose: the service makes blocking LLM calls and sleeps between retries.
        # FastAPI runs sync handlers on a thread pool; an `async def` here would stall the
        # event loop and serialize every team's requests behind one slow call.
        def handler(req: model_cls, account: TeamAccount = Depends(current_team)):  # type: ignore
            check_caller(req, account)
            try:
                resp = getattr(service, method_name)(req, account)
            except QuotaExceeded:
                raise
            except Exception as exc:  # unexpected -> counted against availability SLA
                service.metrics.inc("gateway_errors_total", status="5xx")
                log.exception("gateway %s failed for team=%s", path, account.team)
                raise HTTPException(status_code=500, detail=f"gateway error: {type(exc).__name__}")
            return resp
        handler.__annotations__["req"] = model_cls
        app.post(f"/v1/{path}", name=path)(handler)

    endpoint("extract", ExtractRequest, "extract")
    endpoint("classify", ClassifyRequest, "classify")
    endpoint("summarize", SummarizeRequest, "summarize")
    endpoint("validate", ValidateRequest, "validate")
    endpoint("score", ScoreRequest, "score")

    @app.get("/v1/usage")
    def usage(account: TeamAccount = Depends(current_team)):
        return {"team": account.team, "calls_today": service.quota.used(account.team),
                "daily_quota": account.daily_quota, "spend_usd": round(service.cost.total(account.team), 6),
                "monthly_budget_usd": account.monthly_budget_usd}

    @app.get("/v1/cost-report")
    def cost_report(account: TeamAccount = Depends(current_team)):
        full = service.cost.report()   # callers only see their own team's slice
        return {"team": account.team, "report": full["by_team"].get(account.team, {}),
                "by_pipeline": {k: v for k, v in full["by_pipeline"].items()
                                if k.startswith(account.team + "/")}}

    @app.get("/health")
    def health():
        return {"status": "ok", "circuit_breaker": service.breaker.state,
                "error_rate": round(service.breaker.error_rate(), 4)}

    @app.get("/metrics")
    def metrics():
        return service.metrics.snapshot()

    return app
