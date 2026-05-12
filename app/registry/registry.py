"""Dynamic registry of upstream MCP servers.

Tool naming
-----------
All tools are namespaced to prevent collisions when multiple upstreams expose
identically-named tools. The `NamingConfig.strategy` controls this:

  prefix_always     — <server_id>.<tool>  (default, unambiguous)
  prefix_on_conflict — bare name when globally unique across all healthy
                       servers; prefixed otherwise
  bare              — no prefix; collision policy decides the outcome

The `NamingConfig.on_conflict` policy fires when two tools would share the
same qualified name:

  warn   — log a warning; first-registered server wins (existing entry kept)
  error  — raise ValueError; second registration is rejected
  suffix — append _<server_id> to the later tool: search_internal
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import CircuitBreakerPolicy, NamingConfig, UpstreamServer
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


class ToolNameConflict(Exception):
    pass


class ServerRegistry:
    def __init__(
        self,
        cb_policy: CircuitBreakerPolicy | None = None,
        naming: NamingConfig | None = None,
    ) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        )
        _cb_policy = cb_policy or CircuitBreakerPolicy()
        self._cb_registry = CircuitBreakerRegistry(
            enabled=_cb_policy.enabled,
            failure_threshold=_cb_policy.failure_threshold,
            recovery_timeout=_cb_policy.recovery_timeout,
            probe_successes=_cb_policy.probe_successes,
        )
        self._naming = naming or NamingConfig()

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
        """Resolve a (possibly qualified) tool name to (entry, raw_tool_name).

        Tries in order:
          1. Exact match on ToolDefinition.qualified_name (covers all strategies).
          2. Prefix split: "pg.list_tables" → prefix "pg".
          3. Bare name — only succeeds when the name is unambiguous (exactly one
             match); returns None for collisions so the caller gets a clean 404
             rather than a random server.
        """
        # 1. Exact qualified_name match
        for entry in self._entries.values():
            for td in entry.tools:
                if td.qualified_name == qualified_name:
                    return entry, td.name

        # 2. Prefix split (handles the common "server_id.tool_name" pattern)
        if "." in qualified_name:
            prefix, _, raw = qualified_name.partition(".")
            for entry in self._entries.values():
                effective_prefix = entry.server.tool_prefix or entry.server.id
                if effective_prefix == prefix:
                    return entry, raw

        # 3. Bare name — must be globally unique
        matches = [
            (entry, qualified_name)
            for entry in self._entries.values()
            for td in entry.tools
            if td.name == qualified_name
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            servers = [e.server.id for e, _ in matches]
            log.warning(
                "registry.ambiguous_tool",
                tool=qualified_name,
                servers=servers,
                hint="use qualified name e.g. '{}.{}'".format(servers[0], qualified_name),
            )
        return None

    def list_conflicts(self) -> list[dict[str, Any]]:
        """Return all bare tool names that appear in more than one server."""
        seen: dict[str, list[str]] = {}
        for entry in self._entries.values():
            if not entry.server.enabled:
                continue
            for td in entry.tools:
                seen.setdefault(td.name, []).append(entry.server.id)
        return [
            {"tool": name, "servers": sids}
            for name, sids in seen.items()
            if len(sids) > 1
        ]

    def circuit_breaker_states(self) -> dict[str, dict]:
        return self._cb_registry.all_states()

    # ------------- sync & naming -------------

    async def sync_one(self, server_id: str) -> bool:
        entry = self._entries.get(server_id)
        if entry is None or not entry.server.enabled:
            return False
        try:
            raw_tools = await entry.client.list_tools()
            tools = self._build_tool_definitions(entry, raw_tools)
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

    def _build_tool_definitions(
        self, entry: RegistryEntry, raw_tools: list[dict]
    ) -> list[ToolDefinition]:
        """Apply naming strategy and collision policy, returning ToolDefinitions."""
        strategy = self._naming.strategy
        on_conflict = self._naming.on_conflict
        prefix = entry.server.tool_prefix or entry.server.id
        mutable_set = set(entry.server.mutable_tools)

        # Collect all qualified names already claimed by OTHER servers.
        existing: dict[str, str] = {}  # qualified_name → server_id
        for sid, other in self._entries.items():
            if sid == entry.server.id:
                continue
            for td in other.tools:
                if td.qualified_name:
                    existing[td.qualified_name] = sid

        tools: list[ToolDefinition] = []
        for raw in raw_tools:
            from app.models.mcp import ToolDefinition as TD
            td = TD.model_validate(raw)
            td.server_id = entry.server.id
            td.mutable = td.name in mutable_set
            td.tags = list(entry.server.tags)

            # Decide the qualified name.
            if strategy == "prefix_always":
                qualified = f"{prefix}.{td.name}"
            elif strategy == "bare":
                qualified = td.name
            else:  # prefix_on_conflict
                bare = td.name
                # Check if any other server also has this bare tool name.
                collides = any(
                    any(t.name == bare for t in other.tools)
                    for sid, other in self._entries.items()
                    if sid != entry.server.id
                )
                qualified = f"{prefix}.{bare}" if collides else bare

            # Apply collision policy if the qualified name is already taken.
            if qualified in existing:
                owner = existing[qualified]
                if on_conflict == "error":
                    raise ToolNameConflict(
                        f"tool '{qualified}' already registered by server '{owner}'; "
                        f"cannot register from '{entry.server.id}'"
                    )
                elif on_conflict == "suffix":
                    qualified = f"{qualified}_{entry.server.id}"
                    log.warning(
                        "registry.naming.conflict.suffix",
                        original=f"{qualified}_{entry.server.id}",
                        renamed=qualified,
                        owner=owner,
                        new_server=entry.server.id,
                    )
                else:  # warn — first wins
                    log.warning(
                        "registry.naming.conflict.skipped",
                        tool=qualified,
                        owner=owner,
                        skipped=entry.server.id,
                    )
                    continue  # Don't add this tool; owner keeps it.

            td.qualified_name = qualified
            existing[qualified] = entry.server.id
            tools.append(td)

        return tools

    async def sync_all(self) -> None:
        await asyncio.gather(
            *(self.sync_one(sid) for sid in list(self._entries)),
            return_exceptions=True,
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
                "auth_strategy": (
                    e.server.server_auth.strategy if e.server.server_auth
                    else ("static" if e.server.auth_header else "none")
                ),
            }
            for sid, e in self._entries.items()
        }
