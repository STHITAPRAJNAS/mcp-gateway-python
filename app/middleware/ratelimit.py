"""Token-bucket rate limiter.

Two independent limiters are enforced per call:
  * Per-agent global limiter   — caps total tool calls/sec for an agent.
  * Per-(agent, tool) limiter  — caps calls to a specific tool.

Each bucket refills continuously at `rate` tokens/sec up to `burst` capacity.
The implementation is in-process (asyncio-safe via a lock); for multi-node
deployments swap the store with a Redis Lua-script backend.

Config shape (gateway.yaml):
  rate_limiting:
    enabled: true
    default_agent_rate: 10          # tokens/sec
    default_agent_burst: 20
    default_tool_rate: 5
    default_tool_burst: 10
    # Per-agent overrides
    agents:
      ops-bot:
        agent_rate: 50
        agent_burst: 100
    # Per-tool overrides
    tools:
      pg.execute_sql:
        rate: 2
        burst: 4
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


class RateLimitExceeded(Exception):
    def __init__(self, scope: str, retry_after: float) -> None:
        super().__init__(f"rate limit exceeded for {scope}")
        self.scope = scope
        self.retry_after = retry_after


@dataclass
class TokenBucket:
    rate: float        # tokens added per second
    capacity: float    # max tokens (burst)
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last = time.monotonic()

    async def consume(self, tokens: float = 1.0) -> tuple[bool, float]:
        """Try to consume `tokens`. Returns (allowed, retry_after_seconds)."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, 0.0
            wait = (tokens - self._tokens) / self.rate
            return False, round(wait, 3)


@dataclass
class RateLimiterConfig:
    enabled: bool = True
    default_agent_rate: float = 10.0
    default_agent_burst: float = 20.0
    default_tool_rate: float = 5.0
    default_tool_burst: float = 10.0
    agents: dict[str, dict[str, float]] = field(default_factory=dict)
    tools: dict[str, dict[str, float]] = field(default_factory=dict)


class RateLimiter:
    """Manages a registry of token buckets."""

    def __init__(self, cfg: RateLimiterConfig) -> None:
        self.cfg = cfg
        self._agent_buckets: dict[str, TokenBucket] = {}
        self._tool_buckets: dict[str, TokenBucket] = {}

    def _agent_bucket(self, agent_id: str) -> TokenBucket:
        if agent_id not in self._agent_buckets:
            override = self.cfg.agents.get(agent_id, {})
            self._agent_buckets[agent_id] = TokenBucket(
                rate=override.get("agent_rate", self.cfg.default_agent_rate),
                capacity=override.get("agent_burst", self.cfg.default_agent_burst),
            )
        return self._agent_buckets[agent_id]

    def _tool_bucket(self, agent_id: str, qualified_tool: str) -> TokenBucket:
        key = f"{agent_id}:{qualified_tool}"
        if key not in self._tool_buckets:
            override = self.cfg.tools.get(qualified_tool, {})
            self._tool_buckets[key] = TokenBucket(
                rate=override.get("rate", self.cfg.default_tool_rate),
                capacity=override.get("burst", self.cfg.default_tool_burst),
            )
        return self._tool_buckets[key]

    async def check(self, agent_id: str, qualified_tool: str) -> None:
        if not self.cfg.enabled:
            return
        # Check agent global limit first.
        allowed, retry = await self._agent_bucket(agent_id).consume()
        if not allowed:
            raise RateLimitExceeded(f"agent:{agent_id}", retry)
        # Check per-(agent, tool) limit.
        allowed, retry = await self._tool_bucket(agent_id, qualified_tool).consume()
        if not allowed:
            raise RateLimitExceeded(f"tool:{agent_id}:{qualified_tool}", retry)

    def stats(self) -> dict[str, Any]:
        return {
            "agent_buckets": len(self._agent_buckets),
            "tool_buckets": len(self._tool_buckets),
        }
