"""Two-tier tool result cache.

Tier 1 — Manifest cache
  The aggregated tools/list manifest is expensive to compute (one sync per
  registered server). We cache it for a configurable TTL so that
  high-frequency GET /v1/tools calls don't hammer every upstream.

Tier 2 — Tool call cache
  Individual tool calls whose results are stable (weather, exchange rates,
  static lookups) can be cached by (qualified_name, arguments_hash).  The
  gateway operator marks cacheable tools in config; everything else is
  pass-through.

Both caches are in-process (thread-safe via cachetools TTLCache + asyncio
Lock).  For multi-replica deployments swap the backing store with Redis by
implementing the same ToolCache interface with an async Redis client.

Config shape:
  cache:
    enabled: true
    manifest_ttl: 60          # seconds; 0 = disabled
    tool_results:
      enabled: true
      default_ttl: 0          # 0 = not cached unless overridden below
      tools:
        weather.get_current:  300   # cache weather for 5 minutes
        fx.get_rate:          60
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class TTLStore:
    """Simple async-safe TTL dict (no external deps)."""

    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[key]
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        async with self._lock:
            self._store[key] = CacheEntry(value=value, expires_at=time.monotonic() + ttl)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    def size(self) -> int:
        return len(self._store)


def _args_hash(arguments: dict[str, Any]) -> str:
    raw = json.dumps(arguments, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class CacheConfig:
    enabled: bool = True
    manifest_ttl: float = 60.0
    tool_results_enabled: bool = True
    default_tool_ttl: float = 0.0
    tool_ttls: dict[str, float] = field(default_factory=dict)


class ToolCache:
    """Orchestrator-level cache for manifests and tool call results."""

    MANIFEST_KEY = "__manifest__"

    def __init__(self, cfg: CacheConfig) -> None:
        self.cfg = cfg
        self._manifest_store = TTLStore()
        self._result_store = TTLStore()

    # ---- manifest ----

    async def get_manifest(self) -> Any | None:
        if not self.cfg.enabled or self.cfg.manifest_ttl <= 0:
            return None
        return await self._manifest_store.get(self.MANIFEST_KEY)

    async def set_manifest(self, manifest: Any) -> None:
        if not self.cfg.enabled or self.cfg.manifest_ttl <= 0:
            return
        await self._manifest_store.set(self.MANIFEST_KEY, manifest, self.cfg.manifest_ttl)

    async def invalidate_manifest(self) -> None:
        await self._manifest_store.delete(self.MANIFEST_KEY)

    # ---- tool results ----

    def _tool_ttl(self, qualified_name: str) -> float:
        if not self.cfg.tool_results_enabled:
            return 0.0
        return self.cfg.tool_ttls.get(qualified_name, self.cfg.default_tool_ttl)

    def _result_key(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        return f"{qualified_name}:{_args_hash(arguments)}"

    async def get_result(self, qualified_name: str, arguments: dict[str, Any]) -> Any | None:
        if not self.cfg.enabled:
            return None
        ttl = self._tool_ttl(qualified_name)
        if ttl <= 0:
            return None
        return await self._result_store.get(self._result_key(qualified_name, arguments))

    async def set_result(
        self, qualified_name: str, arguments: dict[str, Any], result: Any
    ) -> None:
        if not self.cfg.enabled:
            return
        ttl = self._tool_ttl(qualified_name)
        if ttl <= 0:
            return
        await self._result_store.set(self._result_key(qualified_name, arguments), result, ttl)

    def stats(self) -> dict[str, int]:
        return {
            "manifest_entries": self._manifest_store.size(),
            "result_entries": self._result_store.size(),
        }
