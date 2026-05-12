"""Pydantic v2 models for the MCP-style wire protocol used by the gateway.

These mirror the JSON-RPC 2.0 envelope used by fast-mcp and the broader
Model Context Protocol. We keep them deliberately small — the gateway is
a thin orchestrator and forwards unknown fields opaquely.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class JSONRPCRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Any | None = None


class JSONRPCResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any | None = None
    error: JSONRPCError | None = None


class ToolDefinition(BaseModel):
    """A tool exposed by an upstream MCP server, with gateway qualification."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")
    # Gateway-added metadata
    server_id: str | None = None
    qualified_name: str | None = None
    mutable: bool = False
    tags: list[str] = Field(default_factory=list)


class ToolManifest(BaseModel):
    tools: list[ToolDefinition]
    generated_at: str
    server_count: int


class ToolCallRequest(BaseModel):
    """REST representation of a tool invocation."""

    name: str = Field(..., description="Qualified tool name 'server_id.tool' or bare tool name")
    arguments: dict[str, Any] = Field(default_factory=dict)
    server_id: str | None = Field(
        default=None, description="Optional explicit server id; overrides any prefix in name"
    )


class ToolCallResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    server_id: str
    tool: str
    content: Any
    is_error: bool = False
    latency_ms: float = 0.0
    redacted: bool = False


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    upstreams: dict[str, str]
    version: str = "0.1.0"


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    code: int = 500
