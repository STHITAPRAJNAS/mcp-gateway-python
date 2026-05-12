"""FastAPI application entrypoint.

Wires together all gateway components and manages the application lifecycle:
  * Config loading
  * Registry initialisation (with circuit-breaker policy)
  * OIDC-aware authorizer
  * Rate limiter
  * Audit store (async SQLAlchemy, starts background flush consumer)
  * Background health sweep (periodic asyncio task)
  * Route mounting
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RequestContextMiddleware
from app.api.routes import api_router, public_router
from app.audit.store import AuditStore
from app.config import GatewayConfig, Settings, get_settings
from app.guardrails.safety import SafetyFilter
from app.middleware.auth import Authorizer
from app.middleware.ratelimit import RateLimiter, RateLimiterConfig
from app.middleware.redaction import Redactor
from app.observability.logging import configure_logging, get_logger
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import ServerRegistry


def _build_components(
    cfg: GatewayConfig, settings: Settings
) -> tuple[ServerRegistry, Orchestrator, Authorizer, AuditStore]:
    registry = ServerRegistry(cb_policy=cfg.circuit_breaker, naming=cfg.naming)
    authorizer = Authorizer(cfg.auth)
    redactor = Redactor(cfg.redaction)
    safety = SafetyFilter(cfg.guardrails)
    rl_cfg = RateLimiterConfig(
        enabled=cfg.rate_limiting.enabled,
        default_agent_rate=cfg.rate_limiting.default_agent_rate,
        default_agent_burst=cfg.rate_limiting.default_agent_burst,
        default_tool_rate=cfg.rate_limiting.default_tool_rate,
        default_tool_burst=cfg.rate_limiting.default_tool_burst,
        agents=dict(cfg.rate_limiting.agents),
        tools=dict(cfg.rate_limiting.tools),
    )
    rate_limiter = RateLimiter(rl_cfg)
    audit = AuditStore(settings.audit_db_url)
    orchestrator = Orchestrator(registry, authorizer, redactor, safety, rate_limiter, audit)
    return registry, orchestrator, authorizer, audit


async def _health_sweep(registry: ServerRegistry, interval: float) -> None:
    """Background task: periodically ping all upstreams and refresh health state."""
    log = get_logger("health_sweep")
    while True:
        await asyncio.sleep(interval)
        log.debug("health_sweep.start")
        await registry.sync_all()
        log.debug(
            "health_sweep.done",
            healthy=sum(1 for e in registry.all_entries() if e.healthy),
            total=len(registry.all_entries()),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)
    log = get_logger("gateway")
    cfg = settings.load_gateway_config()

    registry, orchestrator, authorizer, audit = _build_components(cfg, settings)

    app.state.settings = settings
    app.state.config = cfg
    app.state.registry = registry
    app.state.orchestrator = orchestrator
    app.state.authorizer = authorizer
    app.state.audit = audit

    # Start audit store (creates DB schema + background flush consumer).
    await audit.start()

    # Register all configured upstreams.
    for server in cfg.upstreams:
        await registry.register(server, sync=True)

    # Start background health sweep if configured.
    sweep_task: asyncio.Task | None = None
    if settings.health_sweep_interval > 0:
        sweep_task = asyncio.create_task(
            _health_sweep(registry, settings.health_sweep_interval),
            name="health-sweep",
        )

    log.info(
        "gateway.startup",
        upstream_count=len(cfg.upstreams),
        host=settings.host,
        port=settings.port,
        audit_db=settings.audit_db_url,
        health_sweep_interval=settings.health_sweep_interval,
    )

    try:
        yield
    finally:
        log.info("gateway.shutdown")
        if sweep_task is not None:
            sweep_task.cancel()
            try:
                await sweep_task
            except asyncio.CancelledError:
                pass
        await registry.close()
        await audit.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="MCP Gateway",
        version="0.2.0",
        description=(
            "Enterprise control plane for fast-mcp servers: dynamic registry, "
            "tool aggregation, PII redaction, guardrails, rate limiting, "
            "circuit breakers, OIDC auth, immutable audit log, and full observability."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(public_router)
    app.include_router(api_router)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
