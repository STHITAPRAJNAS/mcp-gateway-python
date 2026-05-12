"""Tests for the token-bucket rate limiter."""
import asyncio

import pytest

from app.middleware.ratelimit import RateLimiterConfig, RateLimitExceeded, RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter(
        RateLimiterConfig(
            enabled=True,
            default_agent_rate=2.0,
            default_agent_burst=2.0,
            default_tool_rate=100.0,   # high so tool bucket doesn't interfere
            default_tool_burst=100.0,
        )
    )


async def test_allows_within_burst(limiter):
    # Burst of 2 — first two calls should succeed.
    await limiter.check("agent1", "srv.tool")
    await limiter.check("agent1", "srv.tool")


async def test_blocks_on_exhaustion(limiter):
    await limiter.check("agent1", "srv.tool")
    await limiter.check("agent1", "srv.tool")
    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.check("agent1", "srv.tool")
    assert "agent:agent1" in exc.value.scope
    assert exc.value.retry_after > 0


async def test_agents_have_independent_buckets(limiter):
    await limiter.check("agent1", "srv.tool")
    await limiter.check("agent1", "srv.tool")
    # agent2 has its own bucket — should still pass.
    await limiter.check("agent2", "srv.tool")


async def test_disabled_allows_unlimited():
    limiter = RateLimiter(RateLimiterConfig(enabled=False))
    for _ in range(100):
        await limiter.check("any", "any.tool")


async def test_per_tool_override():
    limiter = RateLimiter(
        RateLimiterConfig(
            enabled=True,
            default_agent_rate=100.0,
            default_agent_burst=100.0,
            default_tool_rate=100.0,
            default_tool_burst=100.0,
            tools={"pg.execute_sql": {"rate": 1.0, "burst": 1.0}},
        )
    )
    await limiter.check("agent1", "pg.execute_sql")
    with pytest.raises(RateLimitExceeded):
        await limiter.check("agent1", "pg.execute_sql")
