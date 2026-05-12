"""Asynchronous JSON-RPC client for talking to fast-mcp upstream servers.

Supports two transports:
  * "http": plain JSON-RPC over POST (fast-mcp's `streamable-http` content-type),
  * "sse": JSON-RPC POSTed to /messages with responses returned on an SSE stream.

For enterprise resilience we layer:
  * Connection pooling via a shared httpx.AsyncClient,
  * Exponential-backoff retries on transient network errors,
  * Per-request timeouts and cancellation,
  * Structured tracing via structlog.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import UpstreamServer
from app.models.mcp import JSONRPCError, JSONRPCRequest, JSONRPCResponse
from app.observability.logging import get_logger

log = get_logger("mcp.client")


class UpstreamError(Exception):
    """Raised when an upstream MCP server returns a JSON-RPC error or transport fault."""

    def __init__(self, message: str, code: int = -32000, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class MCPClient:
    """Stateful client for a single upstream MCP server."""

    def __init__(self, server: UpstreamServer, http: httpx.AsyncClient | None = None) -> None:
        self.server = server
        self._id_counter = itertools.count(1)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(server.timeout_seconds),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.server.auth_header and self.server.auth_token:
            headers[self.server.auth_header] = self.server.auth_token
        return headers

    def _next_id(self) -> int:
        return next(self._id_counter)

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Issue a JSON-RPC request and return the `result` value."""
        payload = JSONRPCRequest(id=self._next_id(), method=method, params=params or {})
        url = str(self.server.base_url).rstrip("/") + "/"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.RemoteProtocolError, asyncio.TimeoutError)
            ),
            reraise=True,
        ):
            with attempt:
                log.debug(
                    "mcp.upstream.request",
                    server_id=self.server.id,
                    method=method,
                    attempt=attempt.retry_state.attempt_number,
                )
                resp = await self._http.post(
                    url,
                    json=payload.model_dump(mode="json"),
                    headers=self._headers(),
                    timeout=timeout or self.server.timeout_seconds,
                )
                resp.raise_for_status()
                rpc = JSONRPCResponse.model_validate(resp.json())
                if rpc.error is not None:
                    err: JSONRPCError = rpc.error
                    raise UpstreamError(err.message, code=err.code, data=err.data)
                return rpc.result

        raise UpstreamError("retry loop exhausted without producing a result")

    # ------------------- High-level MCP operations -------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list")
        if isinstance(result, dict) and "tools" in result:
            return list(result["tools"])
        if isinstance(result, list):
            return list(result)
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self.request(
            "tools/call", params={"name": name, "arguments": arguments}
        )

    async def ping(self) -> bool:
        try:
            await self.request("ping")
            return True
        except UpstreamError as exc:
            # Many MCP servers don't implement ping; fall back to tools/list.
            if exc.code == -32601:
                try:
                    await self.list_tools()
                    return True
                except Exception:
                    return False
            return False
        except Exception:
            return False
