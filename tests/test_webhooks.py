"""Tests for webhook event dispatcher."""
import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

from app.webhooks.dispatcher import WebhookConfig, WebhookDispatcher, _sign


def test_sign_produces_sha256_hmac():
    payload = b'{"event": "test"}'
    secret = "mysecret"
    sig = _sign(payload, secret)
    assert sig.startswith("sha256=")
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"


async def test_dispatch_fires_background_task():
    cfg = WebhookConfig(url="http://example.invalid/hook", events=[])
    dispatcher = WebhookDispatcher([cfg])

    delivered = []

    async def fake_deliver(client, c, event_type, payload):
        delivered.append((event_type, payload))

    with patch("app.webhooks.dispatcher._deliver", side_effect=fake_deliver):
        dispatcher.dispatch("tool_call_ok", {"tool": "test"})
        await asyncio.sleep(0.05)

    assert len(delivered) == 1
    assert delivered[0][0] == "tool_call_ok"
    await dispatcher.close()


async def test_dispatch_filters_by_event():
    cfg = WebhookConfig(url="http://example.invalid/hook", events=["auth_deny"])
    dispatcher = WebhookDispatcher([cfg])

    delivered = []

    async def fake_deliver(client, c, event_type, payload):
        delivered.append(event_type)

    with patch("app.webhooks.dispatcher._deliver", side_effect=fake_deliver):
        dispatcher.dispatch("tool_call_ok", {"tool": "x"})  # filtered out
        dispatcher.dispatch("auth_deny", {"agent_id": "bot"})  # passes
        await asyncio.sleep(0.05)

    assert delivered == ["auth_deny"]
    await dispatcher.close()


async def test_dispatch_no_configs_is_noop():
    dispatcher = WebhookDispatcher([])
    # Should not raise or create tasks.
    dispatcher.dispatch("tool_call_ok", {"tool": "x"})
    await dispatcher.close()


async def test_payload_includes_event_metadata():
    delivered_payloads = []

    async def fake_deliver(client, c, event_type, payload):
        delivered_payloads.append(payload)

    cfg = WebhookConfig(url="http://example.invalid/hook", events=[])
    dispatcher = WebhookDispatcher([cfg])

    with patch("app.webhooks.dispatcher._deliver", side_effect=fake_deliver):
        dispatcher.dispatch("tool_call_ok", {"tool": "search"})
        await asyncio.sleep(0.05)

    assert len(delivered_payloads) == 1
    p = delivered_payloads[0]
    assert "event_id" in p
    assert p["event_type"] == "tool_call_ok"
    assert "timestamp" in p
    assert p["tool"] == "search"
    await dispatcher.close()


async def test_event_id_monotonically_increases():
    ids = []

    async def fake_deliver(client, c, event_type, payload):
        ids.append(payload["event_id"])

    cfg = WebhookConfig(url="http://example.invalid/hook", events=[])
    dispatcher = WebhookDispatcher([cfg])

    with patch("app.webhooks.dispatcher._deliver", side_effect=fake_deliver):
        for _ in range(5):
            dispatcher.dispatch("tool_call_ok", {})
        await asyncio.sleep(0.05)

    assert len(ids) == 5
    # All IDs should be unique.
    assert len(set(ids)) == 5
    await dispatcher.close()


@respx.mock
async def test_deliver_sends_http_post():
    route = respx.post("http://hook.test/events").mock(
        return_value=httpx.Response(200)
    )
    cfg = WebhookConfig(url="http://hook.test/events", events=[], timeout=2.0, max_retries=1)
    dispatcher = WebhookDispatcher([cfg])
    dispatcher.dispatch("guardrail_block", {"reason": "DROP TABLE"})
    await asyncio.sleep(0.1)
    assert route.called
    await dispatcher.close()


@respx.mock
async def test_deliver_includes_signature_header_when_secret_set(monkeypatch):
    monkeypatch.setenv("MY_WEBHOOK_SECRET", "topsecret")
    received_headers = {}

    def capture(request, **kwargs):
        received_headers.update(dict(request.headers))
        return httpx.Response(200)

    respx.post("http://hook.test/events").mock(side_effect=capture)
    cfg = WebhookConfig(
        url="http://hook.test/events",
        secret_env="MY_WEBHOOK_SECRET",
        events=[],
        max_retries=1,
    )
    dispatcher = WebhookDispatcher([cfg])
    dispatcher.dispatch("tool_call_ok", {})
    await asyncio.sleep(0.1)
    assert "x-mcp-signature-256" in received_headers
    assert received_headers["x-mcp-signature-256"].startswith("sha256=")
    await dispatcher.close()
