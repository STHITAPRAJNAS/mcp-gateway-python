"""Integration test: full gateway → real fast-mcp server, in-process.

Spins up a minimal FastMCP app via Starlette's TestClient (ASGI), then
points our MCPClient at it over a real HTTP server started with uvicorn in a
background thread — proving the initialize handshake, session management, and
content-block extraction all work against a genuine fast-mcp server.
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest
import uvicorn

import fastmcp

# ----------- build a minimal fast-mcp server -----------

mcp_server = fastmcp.FastMCP("integration-test")


@mcp_server.tool()
def echo(message: str) -> str:
    """Echo a message back."""
    return f"echo: {message}"


@mcp_server.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp_server.tool()
def fail_tool() -> str:
    """Always raises."""
    raise RuntimeError("intentional failure")


# ----------- fixtures -----------


@pytest.fixture(scope="module")
def fastmcp_url():
    """Start a real uvicorn server serving the fast-mcp app; yield its base URL."""
    asgi_app = mcp_server.http_app(transport="http")

    config = uvicorn.Config(asgi_app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to bind.
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start in time"

    # Resolve the actual port (port=0 lets the OS pick).
    sockets = server.servers[0].sockets
    port = sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
async def mcp_client(fastmcp_url):
    from app.config import UpstreamServer
    from app.transport.mcp_client import MCPClient

    server = UpstreamServer(
        id="integration",
        name="integration",
        base_url=fastmcp_url,
        transport="streamable-http",
    )
    client = MCPClient(server)
    yield client
    await client.close()


# ----------- tests -----------


async def test_list_tools_returns_registered_tools(mcp_client):
    tools = await mcp_client.list_tools()
    names = {t["name"] for t in tools}
    assert {"echo", "add", "fail_tool"} <= names


async def test_call_tool_echo(mcp_client):
    result = await mcp_client.call_tool("echo", {"message": "hello gateway"})
    assert "hello gateway" in result


async def test_call_tool_add(mcp_client):
    result = await mcp_client.call_tool("add", {"a": 3, "b": 4})
    # fast-mcp returns the result as a text block containing the string repr.
    assert "7" in str(result)


async def test_call_tool_error_surfaces_as_upstream_error(mcp_client):
    from app.transport.mcp_client import UpstreamError

    with pytest.raises(UpstreamError):
        await mcp_client.call_tool("fail_tool", {})


async def test_session_reused_across_calls(mcp_client):
    """Two successive calls should reuse the same session (not re-initialize)."""
    await mcp_client.call_tool("echo", {"message": "first"})
    session_before = mcp_client._session
    await mcp_client.call_tool("echo", {"message": "second"})
    assert mcp_client._session is session_before


async def test_session_resets_after_close(mcp_client):
    await mcp_client.list_tools()
    assert mcp_client._session is not None
    await mcp_client.close()
    assert mcp_client._session is None
