"""MCP-protocol-compliant client using the official MCP Python SDK.

Each MCPClient manages a single upstream fast-mcp server. On first use it:
  1. Resolves auth headers via the configured AuthProvider (static/env/oauth2).
  2. Opens a streamable-HTTP (2025-03-26) or SSE (2024-11-05) connection.
  3. Runs the `initialize` / `notifications/initialized` handshake.
  4. Caches the live ClientSession for re-use across calls.

Auth headers are re-fetched on every session open so that rotated credentials
(env injection, OAuth token refresh) are always picked up.

Per-call agent identity forwarding: if `server_auth.forward_agent_id_header`
is set, the calling agent's id is forwarded to the upstream so its audit logs
can attribute the call to the originating agent.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import UpstreamServer
from app.observability.logging import get_logger
from app.transport.auth_provider import AuthProvider, AuthProviderError, build_auth_provider
from app.transport.circuit_breaker import CircuitBreaker, CircuitOpenError

log = get_logger("mcp.client")


class UpstreamError(Exception):
    def __init__(self, message: str, code: int = -32000, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _extract_content(result: CallToolResult) -> Any:
    """Flatten MCP content blocks into a JSON-serialisable value."""
    blocks = result.content or []
    parts: list[Any] = []
    for block in blocks:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            parts.append({"type": "image", "mimeType": block.mimeType, "data": block.data})
        elif isinstance(block, EmbeddedResource):
            parts.append({"type": "resource", "resource": block.resource.model_dump(mode="json")})
        else:
            parts.append(str(block))

    payload = parts[0] if len(parts) == 1 else parts

    if result.isError:
        raise UpstreamError(str(payload), code=-32603)

    return payload


class MCPClient:
    """Protocol-correct client for a fast-mcp upstream server."""

    def __init__(
        self,
        server: UpstreamServer,
        http: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.server = server
        self._http = http
        self._cb = circuit_breaker
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

        # Build auth provider from structured config, falling back to legacy fields.
        if server.server_auth:
            self._auth_provider: AuthProvider | None = build_auth_provider(server.server_auth)
            self._forward_agent_header: str | None = server.server_auth.forward_agent_id_header
        elif server.auth_header and server.auth_token:
            from app.transport.auth_provider import StaticAuthProvider
            self._auth_provider = StaticAuthProvider(server.auth_header, server.auth_token)
            self._forward_agent_header = None
        else:
            self._auth_provider = None
            self._forward_agent_header = None

    # ---------- auth ----------

    async def _resolve_headers(self, agent_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._auth_provider is not None:
            try:
                headers.update(await self._auth_provider.get_headers(agent_id))
            except AuthProviderError as exc:
                raise UpstreamError(f"auth provider error: {exc}", code=-32002) from exc
        if self._forward_agent_header and agent_id:
            headers[self._forward_agent_header] = agent_id
        return headers

    # ---------- session lifecycle ----------

    async def _open_session(self, agent_id: str | None = None) -> ClientSession:
        stack = AsyncExitStack()
        url = str(self.server.base_url).rstrip("/")
        transport = self.server.transport
        extra_headers = await self._resolve_headers(agent_id)

        if transport == "sse":
            sse_url = f"{url}/sse"
            read, write = await stack.enter_async_context(
                sse_client(sse_url, headers=extra_headers)
            )
        else:
            # streamable-http (default, fast-mcp 2.x)
            mcp_url = f"{url}/mcp"
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(mcp_url, http_client=self._http)
            )

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._stack = stack
        self._session = session
        log.info(
            "mcp.session.opened",
            server_id=self.server.id,
            transport=transport,
            url=url,
        )
        return session

    async def _get_session(self, agent_id: str | None = None) -> ClientSession:
        async with self._lock:
            if self._session is None:
                self._session = await self._open_session(agent_id)
        return self._session

    async def _reset_session(self) -> None:
        async with self._lock:
            self._session = None
            if self._stack is not None:
                try:
                    await self._stack.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._stack = None

    async def close(self) -> None:
        await self._reset_session()

    # ---------- circuit-breaker ----------

    async def _gate(self) -> None:
        if self._cb is not None:
            try:
                await self._cb.before_call()
            except CircuitOpenError:
                raise UpstreamError(
                    f"circuit open for '{self.server.id}' — try again later",
                    code=-32001,
                )

    async def _on_ok(self) -> None:
        if self._cb is not None:
            await self._cb.on_success()

    async def _on_err(self) -> None:
        if self._cb is not None:
            await self._cb.on_failure()

    # ---------- public API ----------

    async def list_tools(self) -> list[dict[str, Any]]:
        await self._gate()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, ConnectionError, asyncio.TimeoutError)
                ),
                reraise=True,
            ):
                with attempt:
                    if attempt.retry_state.attempt_number > 1:
                        await self._reset_session()
                    session = await self._get_session()
                    result = await session.list_tools()
                    tools = result.tools
        except UpstreamError:
            await self._on_err()
            raise
        except Exception as exc:  # noqa: BLE001
            await self._on_err()
            await self._reset_session()
            raise UpstreamError(str(exc)) from exc

        await self._on_ok()
        return [_tool_to_dict(t) for t in tools]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, agent_id: str | None = None
    ) -> Any:
        await self._gate()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
                retry=retry_if_exception_type(
                    (httpx.TransportError, ConnectionError, asyncio.TimeoutError)
                ),
                reraise=True,
            ):
                with attempt:
                    if attempt.retry_state.attempt_number > 1:
                        await self._reset_session()
                    session = await self._get_session(agent_id)
                    result = await session.call_tool(name, arguments=arguments)
                    content = _extract_content(result)
        except UpstreamError:
            await self._on_err()
            raise
        except Exception as exc:  # noqa: BLE001
            await self._on_err()
            await self._reset_session()
            raise UpstreamError(str(exc)) from exc

        await self._on_ok()
        return content

    async def ping(self) -> bool:
        try:
            session = await self._get_session()
            await session.send_ping()
            return True
        except Exception:  # noqa: BLE001
            await self._reset_session()
            return False


def _tool_to_dict(t: Tool) -> dict[str, Any]:
    schema = t.inputSchema
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="json")
    elif not isinstance(schema, dict):
        schema = {}
    return {"name": t.name, "description": t.description or "", "inputSchema": schema}
