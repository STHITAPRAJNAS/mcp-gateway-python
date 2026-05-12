"""Tests for tool naming strategies and collision detection."""
from __future__ import annotations

import pytest

from app.config import NamingConfig, UpstreamServer
from app.models.mcp import ToolDefinition
from app.registry.registry import RegistryEntry, ServerRegistry, ToolNameConflict
from app.transport.mcp_client import MCPClient
from tests.conftest import FakeClient

_TOOL = {"name": "search", "description": "search", "inputSchema": {}}
_TOOL2 = {"name": "list", "description": "list", "inputSchema": {}}


def _server(sid: str) -> UpstreamServer:
    return UpstreamServer(id=sid, name=sid, base_url="http://x.invalid")


async def _add(reg: ServerRegistry, server: UpstreamServer, fake: FakeClient) -> None:
    """Install a fake client and run through _build_tool_definitions so the
    naming strategy is exercised (unlike _install_fake which bypasses it)."""
    client = fake  # type: ignore[assignment]
    entry = RegistryEntry(server=server, client=client, healthy=True)  # type: ignore[arg-type]
    reg._entries[server.id] = entry  # noqa: SLF001
    raw_tools = await fake.list_tools()
    entry.tools = reg._build_tool_definitions(entry, raw_tools)  # noqa: SLF001


# ---------- prefix_always (default) ----------

async def test_prefix_always_qualifies_all():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_always"))
    await _add(reg, _server("web"), FakeClient([_TOOL]))
    await _add(reg, _server("internal"), FakeClient([_TOOL]))

    names = {td.qualified_name for e in reg.all_entries() for td in e.tools}
    assert names == {"web.search", "internal.search"}


async def test_prefix_always_resolves_qualified():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_always"))
    await _add(reg, _server("web"), FakeClient([_TOOL]))
    await _add(reg, _server("internal"), FakeClient([_TOOL]))

    result = reg.find_by_qualified_tool("web.search")
    assert result is not None
    entry, raw = result
    assert entry.server.id == "web"
    assert raw == "search"


# ---------- bare name ambiguity ----------

async def test_bare_name_returns_none_on_collision():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_always"))
    await _add(reg, _server("web"), FakeClient([_TOOL]))
    await _add(reg, _server("internal"), FakeClient([_TOOL]))

    # Bare "search" is ambiguous — must return None, not pick one at random.
    assert reg.find_by_qualified_tool("search") is None


async def test_bare_name_resolves_when_unique():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_always"))
    await _add(reg, _server("web"), FakeClient([_TOOL]))
    await _add(reg, _server("internal"), FakeClient([_TOOL2]))  # different tool

    result = reg.find_by_qualified_tool("search")
    assert result is not None
    assert result[0].server.id == "web"


# ---------- prefix_on_conflict ----------

async def test_prefix_on_conflict_uses_bare_when_unique():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_on_conflict"))
    await _add(reg, _server("pg"), FakeClient([_TOOL2]))  # only "list"
    names = {td.qualified_name for e in reg.all_entries() for td in e.tools}
    assert "list" in names  # bare, no prefix needed


async def test_prefix_on_conflict_adds_prefix_when_duplicate():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_on_conflict"))
    # Register first server, then second — second should prefix because of conflict.
    await _add(reg, _server("web"), FakeClient([_TOOL]))
    # Re-sync after second server exists so conflict detection fires.
    await _add(reg, _server("internal"), FakeClient([_TOOL]))
    await reg.sync_one("web")  # rebuild with knowledge of both servers

    names = {td.qualified_name for e in reg.all_entries() for td in e.tools}
    # At least one should be prefixed once both are present.
    assert any("." in n for n in names)


# ---------- on_conflict: warn (first wins) ----------

async def test_conflict_warn_first_wins():
    reg = ServerRegistry(
        naming=NamingConfig(strategy="bare", on_conflict="warn")
    )
    await _add(reg, _server("first"), FakeClient([_TOOL]))
    await _add(reg, _server("second"), FakeClient([_TOOL]))

    # The "search" tool should exist exactly once in the manifest (first wins).
    all_tools = [td for e in reg.all_entries() for td in e.tools if td.qualified_name == "search"]
    assert len(all_tools) == 1
    assert all_tools[0].server_id == "first"


# ---------- on_conflict: error ----------

async def test_conflict_error_raises():
    reg = ServerRegistry(
        naming=NamingConfig(strategy="bare", on_conflict="error")
    )
    await _add(reg, _server("first"), FakeClient([_TOOL]))
    with pytest.raises(ToolNameConflict):
        await _add(reg, _server("second"), FakeClient([_TOOL]))


# ---------- on_conflict: suffix ----------

async def test_conflict_suffix_renames_later_tool():
    reg = ServerRegistry(
        naming=NamingConfig(strategy="bare", on_conflict="suffix")
    )
    await _add(reg, _server("first"), FakeClient([_TOOL]))
    await _add(reg, _server("second"), FakeClient([_TOOL]))

    all_names = {td.qualified_name for e in reg.all_entries() for td in e.tools}
    assert "search" in all_names               # first keeps bare name
    assert "search_second" in all_names        # second gets suffixed


# ---------- list_conflicts ----------

async def test_list_conflicts_detects_duplicates():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_always"))
    await _add(reg, _server("a"), FakeClient([_TOOL]))
    await _add(reg, _server("b"), FakeClient([_TOOL]))

    conflicts = reg.list_conflicts()
    assert any(c["tool"] == "search" and set(c["servers"]) == {"a", "b"} for c in conflicts)


async def test_list_conflicts_empty_when_no_duplicates():
    reg = ServerRegistry(naming=NamingConfig(strategy="prefix_always"))
    await _add(reg, _server("a"), FakeClient([_TOOL]))
    await _add(reg, _server("b"), FakeClient([_TOOL2]))

    assert reg.list_conflicts() == []
