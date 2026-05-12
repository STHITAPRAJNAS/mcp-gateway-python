"""FastAPI application entrypoint.

Wires together: config -> registry -> middleware -> orchestrator -> routes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RequestContextMiddleware
from app.api.routes import api_router, public_router
from app.config import GatewayConfig, Settings, get_settings
from app.guardrails.safety import SafetyFilter
from app.middleware.auth import Authorizer
from app.middleware.redaction import Redactor
from app.observability.logging import configure_logging, get_logger
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import ServerRegistry


def _build_components(cfg: GatewayConfig) -> tuple[ServerRegistry, Orchestrator, Authorizer]:
    registry = ServerRegistry()
    authorizer = Authorizer(cfg.auth)
    redactor = Redactor(cfg.redaction)
    safety = SafetyFilter(cfg.guardrails)
    orchestrator = Orchestrator(registry, authorizer, redactor, safety)
    return registry, orchestrator, authorizer


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)
    log = get_logger("gateway")
    cfg = settings.load_gateway_config()
    registry, orchestrator, authorizer = _build_components(cfg)

    app.state.settings = settings
    app.state.config = cfg
    app.state.registry = registry
    app.state.orchestrator = orchestrator
    app.state.authorizer = authorizer

    # Register all configured upstreams and warm their tool caches.
    for server in cfg.upstreams:
        await registry.register(server, sync=True)
    log.info(
        "gateway.startup",
        upstream_count=len(cfg.upstreams),
        host=settings.host,
        port=settings.port,
    )
    try:
        yield
    finally:
        log.info("gateway.shutdown")
        await registry.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="MCP Gateway",
        version="0.1.0",
        description=(
            "Enterprise control plane for fast-mcp servers: dynamic registry, "
            "tool aggregation, PII redaction, guardrails, and full observability."
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
    """Entrypoint used by the `mcp-gateway` console script."""
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
