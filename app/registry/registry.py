"""Dynamic registry of upstream MCP servers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import CircuitBreakerPolicy, UpstreamServer
from app.models.mcp import ToolDefinition
from app.observability.logging import get_logger
from app.observability.metrics import REGISTERED_SERVERS, REGISTERED_TOOLS
from app.transport.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry
from app.transport.mcp_client import MCPClient, UpstreamError

log = get_logger("registry")


@dataclass
class RegistryEntry:
    server: UpstreamServer
    client: MCPClient
    tools: list[ToolDefinition] = field(default_factory=list)
    last_synced: datetime | None = None
    last_error: str | None = None
    healthy: bool = False


class ServerRegistry:
    def __init__(self, cb_policy: CircuitBreakerPolicy | None = None) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
        _cb_policy = cb_policy or CircuitBreakerPolicy()
        self._cb_registry = CircuitBreakerRegistry(
            enabled=_cb_policy.enabled,
            failure_threshold=_cb_policy.failure_threshold,
            recovery_timeout=_cb_policy.recovery_timeout,
            probe_successes=_cb_policy.probe_successes,
        )

    # ------------- lifecycle -------------

    async def close(self) -> None:
        async with self._lock:
            for entry in self._entries.values():
                await entry.client.close()
            self._entries.clear()
        await self._http.aclose()

    # ------------- mutation -------------

    async def register(self, server: UpstreamServer, *, sync: bool = True) -> RegistryEntry:
        async with self._lock:
            if server.id in self._entries:
                await self._entries[server.id].client.close()
            cb = self._cb_registry.get(server.id) if self._cb_registry.enabled else None
            client = MCPClient(server, http=self._http, circuit_breaker=cb)
            entry = RegistryEntry(server=server, client=client)
            self._entries[server.id] = entry
            REGISTERED_SERVERS.set(len(self._entries))
        log.info("registry.register", server_id=server.id, base_url=str(server.base_url))
        if sync and server.enabled:
            await self.sync_one(server.id)
        return entry

    async def deregister(self, server_id: str) -> bool:
        async with self._lock:
            entry = self._entries.pop(server_id, None)
            REGISTERED_SERVERS.set(len(self._entries))
        if entry is None:
            return False
        await entry.client.close()
        log.info("registry.deregister", server_id=server_id)
        self._recompute_tool_count()
        return True

    async def set_enabled(self, server_id: str, enabled: bool) -> bool:
        async with self._lock:
            entry = self._entries.get(server_id)
            if entry is None:
                return False
            entry.server = entry.server.model_copy(update={"enabled": enabled})
        log.info("registry.set_enabled", server_id=server_id, enabled=enabled)
        return True

    # ------------- queries -------------

    def get(self, server_id: str) -> RegistryEntry | None:
        return self._entries.get(server_id)

    def all_entries(self) -> list[RegistryEntry]:
        return list(self._entries.values())

    def enabled_entries(self) -> list[RegistryEntry]:
        return [e for e in self._entries.values() if e.server.enabled]

    def find_by_qualified_tool(self, qualified_name: str) -> tuple[RegistryEntry, str] | None:
        if "." in qualified_name:
            prefix, _, raw = qualified_name.partition(".")
            for entry in self._entries.values():
                effective_prefix = entry.server.tool_prefix or entry.server.id
                if effective_prefix == prefix:
                    return entry, raw
        matches = [
            (entry, qualified_name)
            for entry in self._entries.values()
            if any(t.name == qualified_name for t in entry.tools)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def circuit_breaker_states(self) -> dict[str, dict]:
        return self._cb_registry.all_states()

    # ------------- sync -------------

    async def sync_one(self, server_id: str) -> bool:
        entry = self._entries.get(server_id)
        if entry is None or not entry.server.enabled:
            return False
        try:
            raw_tools = await entry.client.list_tools()
            prefix = entry.server.tool_prefix or entry.server.id
            mutable_set = set(entry.server.mutable_tools)
            tools: list[ToolDefinition] = []
            for raw in raw_tools:
                td = ToolDefinition.model_validate(raw)
                td.server_id = entry.server.id
                td.qualified_name = f"{prefix}.{td.name}"
                td.mutable = td.name in mutable_set
                td.tags = list(entry.server.tags)
                tools.append(td)
            entry.tools = tools
            entry.healthy = True
            entry.last_error = None
            entry.last_synced = datetime.now(timezone.utc)
            log.info("registry.sync.ok", server_id=server_id, tool_count=len(tools))
        except (UpstreamError, httpx.HTTPError, Exception) as exc:  # noqa: BLE001
            entry.healthy = False
            entry.last_error = str(exc)
            log.warning("registry.sync.failed", server_id=server_id, error=str(exc))
        self._recompute_tool_count()
        return entry.healthy

    async def sync_all(self) -> None:
        await asyncio.gather(
            *(self.sync_one(sid) for sid in list(self._entries)), return_exceptions=True
        )

    def _recompute_tool_count(self) -> None:
        total = sum(len(e.tools) for e in self._entries.values() if e.server.enabled)
        REGISTERED_TOOLS.set(total)

    def to_dict(self) -> dict[str, Any]:
        cb_states = self.circuit_breaker_states()
        return {
            sid: {
                "id": e.server.id,
                "name": e.server.name,
                "base_url": str(e.server.base_url),
                "enabled": e.server.enabled,
                "healthy": e.healthy,
                "tool_count": len(e.tools),
                "last_synced": e.last_synced.isoformat() if e.last_synced else None,
                "last_error": e.last_error,
                "tags": e.server.tags,
                "circuit_breaker": cb_states.get(sid, {}),
            }
            for sid, e in self._entries.items()
        }
