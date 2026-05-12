"""Tests for two-tier tool result cache."""
import asyncio

import pytest

from app.cache.tool_cache import CacheConfig, ToolCache
from app.models.mcp import ToolDefinition, ToolManifest


@pytest.fixture
def cfg_enabled():
    return CacheConfig(
        enabled=True,
        manifest_ttl=1.0,
        tool_results_enabled=True,
        default_tool_ttl=0.0,
        tool_ttls={"svc.get_price": 1.0},
    )


@pytest.fixture
def cache(cfg_enabled):
    return ToolCache(cfg_enabled)


async def test_manifest_cache_miss_then_hit(cache):
    assert await cache.get_manifest() is None
    manifest = ToolManifest(tools=[], generated_at="now", server_count=0)
    await cache.set_manifest(manifest)
    result = await cache.get_manifest()
    assert result is not None
    assert result.server_count == 0


async def test_manifest_cache_invalidate(cache):
    manifest = ToolManifest(tools=[], generated_at="now", server_count=0)
    await cache.set_manifest(manifest)
    await cache.invalidate_manifest()
    assert await cache.get_manifest() is None


async def test_tool_result_cache_hit(cache):
    args = {"symbol": "AAPL"}
    assert await cache.get_result("svc.get_price", args) is None
    await cache.set_result("svc.get_price", args, {"price": 100})
    result = await cache.get_result("svc.get_price", args)
    assert result == {"price": 100}


async def test_tool_result_not_cached_when_no_ttl(cache):
    # "svc.other_tool" has no explicit TTL and default_tool_ttl=0.
    args = {"x": 1}
    await cache.set_result("svc.other_tool", args, "value")
    assert await cache.get_result("svc.other_tool", args) is None


async def test_tool_result_different_args_different_keys(cache):
    await cache.set_result("svc.get_price", {"symbol": "AAPL"}, {"price": 100})
    await cache.set_result("svc.get_price", {"symbol": "GOOG"}, {"price": 200})
    assert (await cache.get_result("svc.get_price", {"symbol": "AAPL"})) == {"price": 100}
    assert (await cache.get_result("svc.get_price", {"symbol": "GOOG"})) == {"price": 200}


async def test_disabled_cache_always_misses():
    cache = ToolCache(CacheConfig(enabled=False))
    manifest = ToolManifest(tools=[], generated_at="now", server_count=0)
    await cache.set_manifest(manifest)
    assert await cache.get_manifest() is None
    await cache.set_result("svc.get_price", {}, "val")
    assert await cache.get_result("svc.get_price", {}) is None


async def test_ttl_expiry():
    cache = ToolCache(CacheConfig(
        enabled=True,
        manifest_ttl=0.05,  # 50ms
        tool_results_enabled=True,
        default_tool_ttl=0.0,
        tool_ttls={"t": 0.05},
    ))
    await cache.set_result("t", {}, "val")
    assert await cache.get_result("t", {}) == "val"
    await asyncio.sleep(0.1)
    assert await cache.get_result("t", {}) is None


def test_stats(cache):
    stats = cache.stats()
    assert "manifest_entries" in stats
    assert "result_entries" in stats
