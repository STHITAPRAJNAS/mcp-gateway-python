"""Hot config reload: swap gateway.yaml at runtime without dropping sessions.

Two trigger mechanisms:
  1. POSIX signal SIGHUP — standard Unix convention (kill -HUP <pid>).
  2. POST /v1/admin/reload — HTTP API for environments where SIGHUP is
     inconvenient (containers, Windows, tests).

What gets reloaded:
  ✓ Auth policy (OIDC settings, ACLs, mutable_allowed_agents)
  ✓ Redaction policy
  ✓ Guardrails rules
  ✓ Rate limiting policy
  ✓ Circuit breaker thresholds
  ✓ Naming strategy
  ✓ Cache config
  ✓ Webhook endpoints
  ✓ Upstream server list (add new, remove deleted, update existing)

What is NOT reloaded (requires restart):
  ✗ HTTP host / port
  ✗ Audit DB URL (live migration not safe)
  ✗ Log level / format

Active MCP sessions to upstreams are preserved during reload: servers not
present in the new config are deregistered (their sessions closed), servers
present in both old and new are kept alive unless their base_url changes.
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.observability.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

log = get_logger("reload")


async def _do_reload(app: "FastAPI") -> dict[str, Any]:
    """Core reload logic — diff old vs new config and mutate app state."""
    settings = app.state.settings
    old_cfg = app.state.config
    new_cfg = settings.load_gateway_config()

    registry = app.state.registry
    orchestrator = app.state.orchestrator

    # ── Upstreams ──────────────────────────────────────────────────────────
    old_ids = {s.id for s in old_cfg.upstreams}
    new_ids = {s.id for s in new_cfg.upstreams}

    removed = old_ids - new_ids
    added = new_ids - old_ids
    maybe_changed = old_ids & new_ids

    for sid in removed:
        await registry.deregister(sid)

    for server in new_cfg.upstreams:
        if server.id in added:
            await registry.register(server, sync=True)
        elif server.id in maybe_changed:
            old = next(s for s in old_cfg.upstreams if s.id == server.id)
            if old.base_url != server.base_url or old.transport != server.transport:
                # URL changed — must re-open MCP session.
                await registry.deregister(server.id)
                await registry.register(server, sync=True)
            else:
                # Minor changes (tags, mutable_tools, auth) — update in place.
                entry = registry.get(server.id)
                if entry:
                    entry.server = server

    # ── Policies (swap objects in place on orchestrator) ───────────────────
    from app.guardrails.safety import SafetyFilter
    from app.guardrails.schema_validator import ToolSchemaValidator
    from app.middleware.auth import Authorizer
    from app.middleware.ratelimit import RateLimiter, RateLimiterConfig
    from app.middleware.redaction import Redactor
    from app.cache.tool_cache import CacheConfig, ToolCache
    from app.webhooks.dispatcher import WebhookDispatcher

    orchestrator.authorizer = Authorizer(new_cfg.auth)
    orchestrator.redactor = Redactor(new_cfg.redaction)
    orchestrator.safety = SafetyFilter(new_cfg.guardrails)
    orchestrator.schema_validator = ToolSchemaValidator(
        enabled=new_cfg.schema_validation.enabled,
        strict_on_missing_schema=new_cfg.schema_validation.strict_on_missing_schema,
    )

    rl_cfg = RateLimiterConfig(
        enabled=new_cfg.rate_limiting.enabled,
        default_agent_rate=new_cfg.rate_limiting.default_agent_rate,
        default_agent_burst=new_cfg.rate_limiting.default_agent_burst,
        default_tool_rate=new_cfg.rate_limiting.default_tool_rate,
        default_tool_burst=new_cfg.rate_limiting.default_tool_burst,
        agents=dict(new_cfg.rate_limiting.agents),
        tools=dict(new_cfg.rate_limiting.tools),
    )
    orchestrator.rate_limiter = RateLimiter(rl_cfg)

    cc = new_cfg.cache
    orchestrator.cache = ToolCache(CacheConfig(
        enabled=cc.enabled,
        manifest_ttl=cc.manifest_ttl,
        tool_results_enabled=cc.tool_results_enabled,
        default_tool_ttl=cc.default_tool_ttl,
        tool_ttls=dict(cc.tool_ttls),
    ))
    # Invalidate stale manifest immediately.
    await orchestrator.cache.invalidate_manifest()

    # Rebuild webhook dispatcher.
    old_dispatcher = getattr(orchestrator, "webhook_dispatcher", None)
    if old_dispatcher:
        await old_dispatcher.close()
    from app.webhooks.dispatcher import WebhookConfig
    orchestrator.webhook_dispatcher = WebhookDispatcher([
        WebhookConfig(**wh.model_dump()) for wh in new_cfg.webhooks
    ])

    app.state.config = new_cfg
    app.state.authorizer = orchestrator.authorizer

    result = {
        "reloaded_at": datetime.now(timezone.utc).isoformat(),
        "upstreams_added": list(added),
        "upstreams_removed": list(removed),
        "upstreams_updated": list(maybe_changed),
    }
    log.info("config.reloaded", **result)
    return result


def install_sighup_handler(app: "FastAPI") -> None:
    """Register SIGHUP to trigger a hot reload (Unix only)."""
    try:
        loop = asyncio.get_event_loop()

        def _handler() -> None:
            log.info("config.sighup_received")
            asyncio.ensure_future(_do_reload(app))

        loop.add_signal_handler(signal.SIGHUP, _handler)
        log.info("config.sighup_handler_installed")
    except (NotImplementedError, RuntimeError):
        log.warning("config.sighup_not_supported")
