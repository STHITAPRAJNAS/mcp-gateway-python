"""REST + SSE routes for the gateway."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import (
    get_audit,
    get_orchestrator,
    get_principal,
    get_registry,
    require_api_key,
)
from app.audit.store import AuditStore
from app.config import UpstreamServer, get_settings
from app.middleware.auth import AgentPrincipal
from app.models.mcp import (
    HealthResponse,
    ToolCallRequest,
    ToolCallResult,
    ToolManifest,
)
from app.observability.logging import get_logger
from app.observability.metrics import REGISTRY as PROM_REGISTRY
from app.orchestrator.orchestrator import OrchestrationError, Orchestrator
from app.registry.registry import ServerRegistry

log = get_logger("api")

# ----- Public router (health, metrics) -----
public_router = APIRouter()


@public_router.get("/healthz", response_model=HealthResponse, tags=["system"])
async def healthz(registry: ServerRegistry = Depends(get_registry)) -> HealthResponse:
    statuses = {
        e.server.id: ("healthy" if e.healthy else "unhealthy")
        for e in registry.all_entries()
    }
    overall = "ok"
    if statuses and all(v == "unhealthy" for v in statuses.values()):
        overall = "down"
    elif any(v == "unhealthy" for v in statuses.values()):
        overall = "degraded"
    return HealthResponse(status=overall, upstreams=statuses)


@public_router.get("/metrics", tags=["system"])
async def metrics() -> Response:
    data = generate_latest(PROM_REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


# ----- Authenticated router -----
api_router = APIRouter(dependencies=[Depends(require_api_key)])


# --- Registry management ---

@api_router.get("/v1/registry/servers", tags=["registry"])
async def list_servers(registry: ServerRegistry = Depends(get_registry)) -> dict[str, Any]:
    return {"servers": registry.to_dict()}


@api_router.post(
    "/v1/registry/servers",
    status_code=status.HTTP_201_CREATED,
    tags=["registry"],
)
async def register_server(
    server: UpstreamServer,
    registry: ServerRegistry = Depends(get_registry),
) -> dict[str, Any]:
    entry = await registry.register(server, sync=True)
    return {
        "registered": entry.server.id,
        "healthy": entry.healthy,
        "tool_count": len(entry.tools),
        "last_error": entry.last_error,
    }


@api_router.delete("/v1/registry/servers/{server_id}", tags=["registry"])
async def deregister_server(
    server_id: str, registry: ServerRegistry = Depends(get_registry)
) -> dict[str, Any]:
    ok = await registry.deregister(server_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    return {"deregistered": server_id}


@api_router.post("/v1/registry/servers/{server_id}/sync", tags=["registry"])
async def sync_server(
    server_id: str, registry: ServerRegistry = Depends(get_registry)
) -> dict[str, Any]:
    ok = await registry.sync_one(server_id)
    entry = registry.get(server_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    return {
        "server_id": server_id,
        "healthy": ok,
        "tool_count": len(entry.tools),
        "last_error": entry.last_error,
    }


@api_router.post("/v1/registry/servers/{server_id}/enabled", tags=["registry"])
async def set_enabled(
    server_id: str,
    enabled: bool = Query(...),
    registry: ServerRegistry = Depends(get_registry),
) -> dict[str, Any]:
    ok = await registry.set_enabled(server_id, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    return {"server_id": server_id, "enabled": enabled}


@api_router.get("/v1/registry/circuit-breakers", tags=["registry"])
async def circuit_breaker_states(
    registry: ServerRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return {"circuit_breakers": registry.circuit_breaker_states()}


@api_router.get("/v1/registry/conflicts", tags=["registry"])
async def tool_name_conflicts(
    registry: ServerRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List bare tool names that appear in more than one registered server.

    Use this endpoint to detect naming collisions at runtime without having to
    inspect each server's manifest manually. A non-empty list means the gateway
    is relying on qualified names (e.g. 'pg.search') to disambiguate — bare
    calls to those tool names will return 404.
    """
    conflicts = registry.list_conflicts()
    return {"conflicts": conflicts, "count": len(conflicts)}


# --- Tool manifest & calls ---

@api_router.get("/v1/tools", response_model=ToolManifest, tags=["tools"])
async def list_tools(
    tag: str | None = Query(default=None, description="Optional tag filter"),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> ToolManifest:
    return orchestrator.build_manifest(tag=tag)


@api_router.post("/v1/tools/call", response_model=ToolCallResult, tags=["tools"])
async def call_tool(
    payload: ToolCallRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator),
    principal: AgentPrincipal = Depends(get_principal),
) -> ToolCallResult:
    try:
        return await orchestrator.call_tool(
            principal=principal,
            name=payload.name,
            arguments=payload.arguments,
            server_id=payload.server_id,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=exc.code, detail=str(exc)) from exc


@api_router.post("/v1/tools/call/stream", tags=["tools"])
async def call_tool_stream(
    payload: ToolCallRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator),
    principal: AgentPrincipal = Depends(get_principal),
) -> EventSourceResponse:
    """SSE variant: emits start → progress (heartbeat) → result | error."""
    settings = get_settings()

    async def event_source():
        yield {
            "event": "start",
            "data": json.dumps({"tool": payload.name, "agent_id": principal.agent_id}),
        }
        call_task = asyncio.create_task(
            orchestrator.call_tool(
                principal=principal,
                name=payload.name,
                arguments=payload.arguments,
                server_id=payload.server_id,
            )
        )
        try:
            while True:
                try:
                    result = await asyncio.wait_for(asyncio.shield(call_task), timeout=5.0)
                    break
                except asyncio.TimeoutError:
                    yield {"event": "progress", "data": json.dumps({"status": "running"})}
        except OrchestrationError as exc:
            yield {
                "event": "error",
                "data": json.dumps({"code": exc.code, "message": str(exc)}),
            }
            return
        except Exception as exc:  # noqa: BLE001
            yield {
                "event": "error",
                "data": json.dumps({"code": 500, "message": str(exc)}),
            }
            return
        yield {"event": "result", "data": result.model_dump_json()}

    return EventSourceResponse(
        event_source(),
        ping=15,
        headers={"X-Gateway-Timeout": str(settings.request_timeout_seconds)},
    )


# --- Audit log ---

@api_router.get("/v1/audit", tags=["audit"])
async def query_audit(
    agent_id: str | None = Query(default=None),
    server_id: str | None = Query(default=None),
    tool_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    audit: AuditStore = Depends(get_audit),
) -> dict[str, Any]:
    rows = await audit.query(
        agent_id=agent_id,
        server_id=server_id,
        tool_name=tool_name,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"entries": rows, "count": len(rows)}
