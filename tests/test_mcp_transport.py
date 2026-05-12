"""Tests for the MCP-SDK-based transport layer.

These tests exercise the content-extraction and session-reset logic without
requiring a live fast-mcp server.
"""
from __future__ import annotations

import pytest

from mcp.types import CallToolResult, ImageContent, TextContent

from app.transport.mcp_client import UpstreamError, _extract_content


def _text_result(*texts: str, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=t) for t in texts],
        isError=is_error,
    )


def test_single_text_block_returns_string():
    result = _text_result("hello world")
    assert _extract_content(result) == "hello world"


def test_multiple_text_blocks_returns_list():
    result = _text_result("a", "b", "c")
    assert _extract_content(result) == ["a", "b", "c"]


def test_empty_content_returns_empty_list():
    result = CallToolResult(content=[], isError=False)
    assert _extract_content(result) == []


def test_is_error_raises_upstream_error():
    result = _text_result("something went wrong", is_error=True)
    with pytest.raises(UpstreamError) as exc:
        _extract_content(result)
    assert "something went wrong" in str(exc.value)
    assert exc.value.code == -32603


def test_image_block_returns_dict():
    result = CallToolResult(
        content=[ImageContent(type="image", mimeType="image/png", data="abc123")],
        isError=False,
    )
    out = _extract_content(result)
    assert isinstance(out, dict)
    assert out["type"] == "image"
    assert out["mimeType"] == "image/png"


def test_mixed_blocks_returns_list():
    result = CallToolResult(
        content=[
            TextContent(type="text", text="caption"),
            ImageContent(type="image", mimeType="image/jpeg", data="xyz"),
        ],
        isError=False,
    )
    out = _extract_content(result)
    assert isinstance(out, list)
    assert out[0] == "caption"
    assert out[1]["type"] == "image"
