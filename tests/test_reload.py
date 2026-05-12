"""Tests for hot config reload."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from app.admin.reload import _do_reload
from app.cache.tool_cache import CacheConfig, ToolCache
from app.config import (
    AuthPolicy,
    CacheConfig as CfgCacheConfig,
    GatewayConfig,
    GuardrailsPolicy,
    RateLimitingPolicy,
    RedactionPolicy,
    SchemaValidationConfig,
    UpstreamServer,
    WebhookConfig as CfgWebhookConfig,
)
from app.guardrails.safety import SafetyFilter
from app.guardrails.schema_validator import ToolSchemaValidator
from app.middleware.auth import Authorizer
from app.middleware.ratelimit import RateLimiter, RateLimiterConfig
from app.middleware.redaction import Redactor
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import ServerRegistry
from app.webhooks.dispatcher import WebhookDispatcher


def _make_config(upstreams=None, webhooks=None) -> GatewayConfig:
    return GatewayConfig(
        upstreams=upstreams or [],
        auth=AuthPolicy(enabled=False),
        redaction=RedactionPolicy(enabled=False),
        guardrails=GuardrailsPolicy(enabled=False),
        rate_limiting=RateLimitingPolicy(enabled=False),
        schema_validation=SchemaValidationConfig(enabled=False),
        cache=CfgCacheConfig(enabled=False),
        webhooks=webhooks or [],
    )


def _make_app(old_cfg: GatewayConfig, new_cfg: GatewayConfig):
    """Build a minimal fake FastAPI app state for testing _do_reload."""
    registry = ServerRegistry()
    orchestrator = Orchestrator(
        registry=registry,
        authorizer=Authorizer(old_cfg.auth),
        redactor=Redactor(old_cfg.redaction),
        safety=SafetyFilter(old_cfg.guardrails),
        rate_limiter=RateLimiter(RateLimiterConfig(enabled=False)),
        cache=ToolCache(CacheConfig(enabled=False)),
        webhook_dispatcher=WebhookDispatcher([]),
    )

    settings = MagicMock()
    settings.load_gateway_config.return_value = new_cfg

    app = MagicMock()
    app.state.settings = settings
    app.state.config = old_cfg
    app.state.registry = registry
    app.state.orchestrator = orchestrator

    return app, registry, orchestrator


async def test_reload_adds_new_upstream():
    old_cfg = _make_config(upstreams=[])
    new_upstream = UpstreamServer(
        id="svc1", name="svc1", base_url="http://svc1.invalid"
    )
    new_cfg = _make_config(upstreams=[new_upstream])

    app, registry, _ = _make_app(old_cfg, new_cfg)

    # Mock registry.register to avoid real network calls.
    registry.register = AsyncMock()

    result = await _do_reload(app)
    assert "svc1" in result["upstreams_added"]
    registry.register.assert_awaited_once()


async def test_reload_removes_deleted_upstream():
    old_upstream = UpstreamServer(
        id="gone", name="gone", base_url="http://gone.invalid"
    )
    old_cfg = _make_config(upstreams=[old_upstream])
    new_cfg = _make_config(upstreams=[])

    app, registry, _ = _make_app(old_cfg, new_cfg)
    registry.deregister = AsyncMock(return_value=True)

    result = await _do_reload(app)
    assert "gone" in result["upstreams_removed"]
    registry.deregister.assert_awaited_once_with("gone")


async def test_reload_reconnects_when_url_changes():
    old_upstream = UpstreamServer(
        id="svc", name="svc", base_url="http://old.invalid"
    )
    new_upstream = UpstreamServer(
        id="svc", name="svc", base_url="http://new.invalid"
    )
    old_cfg = _make_config(upstreams=[old_upstream])
    new_cfg = _make_config(upstreams=[new_upstream])

    app, registry, _ = _make_app(old_cfg, new_cfg)
    registry.deregister = AsyncMock(return_value=True)
    registry.register = AsyncMock()

    result = await _do_reload(app)
    # URL-changed server appears in maybe_changed, not added/removed.
    assert "svc" in result["upstreams_updated"]
    registry.deregister.assert_awaited()
    registry.register.assert_awaited()


async def test_reload_swaps_policy_objects():
    old_cfg = _make_config()
    new_cfg = _make_config()

    app, registry, orchestrator = _make_app(old_cfg, new_cfg)

    old_authorizer = orchestrator.authorizer
    old_safety = orchestrator.safety

    result = await _do_reload(app)

    # Policies should be new objects (swapped in place).
    assert orchestrator.authorizer is not old_authorizer
    assert orchestrator.safety is not old_safety
    assert "reloaded_at" in result


async def test_reload_invalidates_manifest_cache():
    # Use an enabled cache in new config to verify invalidation.
    old_cfg = _make_config()
    new_cfg = GatewayConfig(
        upstreams=[],
        auth=AuthPolicy(enabled=False),
        redaction=RedactionPolicy(enabled=False),
        guardrails=GuardrailsPolicy(enabled=False),
        rate_limiting=RateLimitingPolicy(enabled=False),
        schema_validation=SchemaValidationConfig(enabled=False),
        cache=CfgCacheConfig(enabled=True, manifest_ttl=60.0),
        webhooks=[],
    )

    app, registry, orchestrator = _make_app(old_cfg, new_cfg)

    # Pre-populate the old cache with a manifest.
    old_cache = ToolCache(CacheConfig(enabled=True, manifest_ttl=60.0))
    from app.models.mcp import ToolManifest
    await old_cache.set_manifest(ToolManifest(tools=[], generated_at="now", server_count=0))
    orchestrator.cache = old_cache

    await _do_reload(app)

    # After reload, a brand-new cache is installed and its manifest slot is empty.
    new_cache = orchestrator.cache
    assert new_cache is not old_cache
    assert await new_cache.get_manifest() is None


async def test_reload_updates_app_state_config():
    old_cfg = _make_config()
    new_cfg = _make_config()

    app, _, _ = _make_app(old_cfg, new_cfg)
    assert app.state.config is old_cfg

    await _do_reload(app)

    assert app.state.config is new_cfg
