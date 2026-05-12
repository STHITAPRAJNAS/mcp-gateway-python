"""End-to-end API tests using FastAPI TestClient with a fake registry."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.audit.store import AuditStore
from app.config import (
    AuthPolicy,
    GatewayConfig,
    GuardrailsPolicy,
    OIDCPolicy,
    RedactionPolicy,
    UpstreamServer,
)
from app.guardrails.safety import SafetyFilter
from app.main import create_app
from app.middleware.auth import Authorizer
from app.middleware.ratelimit import RateLimiter, RateLimiterConfig
from app.middleware.redaction import Redactor
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import ServerRegistry
from tests.conftest import FakeClient, _install_fake


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GATEWAY_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("MCP_GATEWAY_AUDIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/api_test.db")
    from app.config import get_settings

    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        cfg = GatewayConfig(
            upstreams=[],
            auth=AuthPolicy(
                enabled=True,
                oidc=OIDCPolicy(enabled=False),
                mutable_allowed_agents=["ops-bot"],
            ),
            redaction=RedactionPolicy(enabled=False),
            guardrails=GuardrailsPolicy(enabled=False),
        )
        registry = ServerRegistry()
        authorizer = Authorizer(cfg.auth)
        rl = RateLimiter(RateLimiterConfig(enabled=False))
        audit = app.state.audit  # reuse the one started in lifespan
        orch = Orchestrator(
            registry, authorizer, Redactor(cfg.redaction), SafetyFilter(cfg.guardrails),
            rate_limiter=rl, audit=audit,
        )
        srv = UpstreamServer(id="pg", name="pg", base_url="http://x.invalid", mutable_tools=[])

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


def test_circuit_breaker_states_endpoint(client):
    r = client.get("/v1/registry/circuit-breakers")
    assert r.status_code == 200
    assert "circuit_breakers" in r.json()


def test_audit_endpoint_accessible(client):
    r = client.get("/v1/audit")
    assert r.status_code == 200
    assert "entries" in r.json()
