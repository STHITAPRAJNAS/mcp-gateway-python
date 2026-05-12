"""End-to-end API tests using FastAPI TestClient with a fake registry."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import (
    AuthPolicy,
    GatewayConfig,
    GuardrailsPolicy,
    RedactionPolicy,
    UpstreamServer,
)
from app.guardrails.safety import SafetyFilter
from app.main import create_app
from app.middleware.auth import Authorizer
from app.middleware.redaction import Redactor
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import ServerRegistry
from tests.conftest import FakeClient, _install_fake


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Force the lifespan to find no upstreams so it doesn't try real HTTP.
    monkeypatch.setenv("MCP_GATEWAY_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    from app.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        # Replace the orchestrator/registry built by the lifespan with our fakes.
        cfg = GatewayConfig(
            upstreams=[],
            auth=AuthPolicy(enabled=True, mutable_allowed_agents=["ops-bot"]),
            redaction=RedactionPolicy(enabled=False),
            guardrails=GuardrailsPolicy(enabled=False),
        )
        registry = ServerRegistry()
        authorizer = Authorizer(cfg.auth)
        orch = Orchestrator(
            registry, authorizer, Redactor(cfg.redaction), SafetyFilter(cfg.guardrails)
        )
        srv = UpstreamServer(id="pg", name="pg", base_url="http://x.invalid", mutable_tools=[])
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                _install_fake(
                    registry,
                    srv,
                    FakeClient(
                        tools=[
                            {"name": "list_tables", "description": "list", "inputSchema": {}}
                        ],
                        responses={"list_tables": {"rows": ["a"]}},
                    ),
                )
            )
        finally:
            loop.close()

        app.state.config = cfg
        app.state.registry = registry
        app.state.orchestrator = orch
        app.state.authorizer = authorizer
        yield c


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "degraded", "down"}


def test_metrics_exposed(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"mcp_gateway" in r.content


def test_list_tools(client):
    r = client.get("/v1/tools")
    assert r.status_code == 200
    names = [t["qualified_name"] for t in r.json()["tools"]]
    assert "pg.list_tables" in names


def test_call_tool_requires_agent_id(client):
    r = client.post("/v1/tools/call", json={"name": "pg.list_tables", "arguments": {}})
    assert r.status_code == 401


def test_call_tool_success(client):
    r = client.post(
        "/v1/tools/call",
        json={"name": "pg.list_tables", "arguments": {}},
        headers={"X-Agent-Id": "ops-bot"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server_id"] == "pg"
    assert body["tool"] == "list_tables"
