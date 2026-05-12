"""Pytest fixtures."""
from __future__ import annotations

from typing import Any

import pytest

from app.audit.store import AuditStore
from app.config import (
    AuthPolicy,
    GatewayConfig,
    GuardrailsPolicy,
    OIDCPolicy,
    RateLimitingPolicy,
    RedactionPolicy,
    SafetyRule,
    UpstreamServer,
)
from app.guardrails.safety import SafetyFilter
from app.middleware.auth import Authorizer
from app.middleware.ratelimit import RateLimiter, RateLimiterConfig
from app.middleware.redaction import Redactor
from app.models.mcp import ToolDefinition
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import RegistryEntry, ServerRegistry


class FakeClient:
    """In-memory stand-in for MCPClient."""

    def __init__(self, tools: list[dict[str, Any]], responses: dict[str, Any] | None = None):
        self._tools = tools
        self._responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name in self._responses:
            value = self._responses[name]
            if isinstance(value, Exception):
                raise value
            return value
        return {"echo": {"tool": name, "arguments": arguments}}

    async def ping(self) -> bool:
        return True


async def _install_fake(registry: ServerRegistry, server: UpstreamServer, fake: FakeClient) -> None:
    entry = RegistryEntry(server=server, client=fake)  # type: ignore[arg-type]
    registry._entries[server.id] = entry  # noqa: SLF001
    raw_tools = await fake.list_tools()
    prefix = server.tool_prefix or server.id
    mutable_set = set(server.mutable_tools)
    entry.tools = []
    for raw in raw_tools:
        td = ToolDefinition.model_validate(raw)
        td.server_id = server.id
        td.qualified_name = f"{prefix}.{td.name}"
        td.mutable = td.name in mutable_set
        td.tags = list(server.tags)
        entry.tools.append(td)
    entry.healthy = True


@pytest.fixture
def gateway_config() -> GatewayConfig:
    return GatewayConfig(
        upstreams=[],
        auth=AuthPolicy(
            enabled=True,
            oidc=OIDCPolicy(enabled=False),
            mutable_allowed_agents=["ops-bot"],
            agent_permissions={"readonly": ["search.query"]},
        ),
        redaction=RedactionPolicy(enabled=True, redact_requests=True, redact_responses=True),
        guardrails=GuardrailsPolicy(
            enabled=True,
            rules=[
                SafetyRule(
                    name="no-drop",
                    tool="execute_sql",
                    forbidden_substrings=["DROP TABLE"],
                ),
                SafetyRule(
                    name="bounded-limit",
                    tool="*",
                    numeric_ranges={"limit": {"min": 1, "max": 100}},
                ),
            ],
        ),
    )


@pytest.fixture
async def audit_store(tmp_path):
    store = AuditStore(f"sqlite+aiosqlite:///{tmp_path}/audit_test.db")
    await store.start()
    yield store
    await store.stop()


@pytest.fixture
async def orchestrator(gateway_config, audit_store):
    reg = ServerRegistry()
    pg = UpstreamServer(
        id="pg",
        name="pg",
        base_url="http://example.invalid",
        mutable_tools=["execute_sql"],
        tags=["data"],
    )
    search = UpstreamServer(
        id="search", name="search", base_url="http://example.invalid", tags=["search"]
    )
    await _install_fake(
        reg,
        pg,
        FakeClient(
            tools=[
                {"name": "list_tables", "description": "list tables", "inputSchema": {}},
                {"name": "execute_sql", "description": "run sql", "inputSchema": {}},
            ],
            responses={
                "list_tables": {"rows": ["users", "orders"]},
                "execute_sql": {"rows": [{"email": "alice@example.com"}]},
            },
        ),
    )
    await _install_fake(
        reg,
        search,
        FakeClient(
            tools=[{"name": "query", "description": "search", "inputSchema": {}}],
            responses={"query": {"hits": ["doc1"]}},
        ),
    )
    rl = RateLimiter(RateLimiterConfig(enabled=False))
    orch = Orchestrator(
        reg,
        Authorizer(gateway_config.auth),
        Redactor(gateway_config.redaction),
        SafetyFilter(gateway_config.guardrails),
        rate_limiter=rl,
        audit=audit_store,
    )
    yield orch
    await reg.close()
