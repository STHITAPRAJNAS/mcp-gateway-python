"""Webhook event dispatcher.

Fires signed HTTP POST events to configured endpoints on key gateway events:
  tool_call_ok, tool_call_error, guardrail_block, auth_deny,
  rate_limit, schema_invalid, upstream_unavailable.

Delivery guarantees:
  - Fire-and-forget via asyncio background task (never blocks the hot path).
  - Up to `max_retries` attempts with exponential backoff.
  - Each request is signed with HMAC-SHA256 (X-MCP-Signature-256 header)
    so receivers can verify authenticity — the same pattern GitHub uses.

Config shape (gateway.yaml):
  webhooks:
    - url: https://siem.example.com/mcp-events
      secret_env: WEBHOOK_SECRET_1   # env var holding the signing secret
      events:                        # omit to receive all events
        - tool_call_ok
        - tool_call_error
        - guardrail_block
        - auth_deny
      timeout: 5.0
      max_retries: 3
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from app.observability.logging import get_logger

log = get_logger("webhooks")

EventType = Literal[
    "tool_call_ok",
    "tool_call_error",
    "guardrail_block",
    "schema_invalid",
    "auth_deny",
    "rate_limit",
    "upstream_unavailable",
    "server_registered",
    "server_deregistered",
    "config_reloaded",
]

ALL_EVENTS: set[str] = {
    "tool_call_ok", "tool_call_error", "guardrail_block", "schema_invalid",
    "auth_deny", "rate_limit", "upstream_unavailable",
    "server_registered", "server_deregistered", "config_reloaded",
}


@dataclass
class WebhookConfig:
    url: str
    secret_env: str | None = None          # env var holding HMAC secret
    events: list[str] = field(default_factory=list)  # empty = all events
    timeout: float = 5.0
    max_retries: int = 3


def _sign(payload: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


async def _deliver(
    client: httpx.AsyncClient,
    cfg: WebhookConfig,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload, default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "X-MCP-Event": event_type,
        "X-MCP-Delivery": payload.get("event_id", ""),
        "X-MCP-Timestamp": str(int(time.time())),
    }
    secret = os.environ.get(cfg.secret_env, "") if cfg.secret_env else ""
    if secret:
        headers["X-MCP-Signature-256"] = _sign(body, secret)

    delay = 0.5
    for attempt in range(cfg.max_retries):
        try:
            resp = await client.post(cfg.url, content=body, headers=headers, timeout=cfg.timeout)
            if resp.status_code < 500:
                log.debug(
                    "webhook.delivered",
                    url=cfg.url,
                    event=event_type,
                    status=resp.status_code,
                    attempt=attempt + 1,
                )
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook.error", url=cfg.url, attempt=attempt + 1, error=str(exc))
        if attempt < cfg.max_retries - 1:
            await asyncio.sleep(delay)
            delay *= 2


class WebhookDispatcher:
    """Manages a pool of webhook endpoints and dispatches events to them."""

    def __init__(self, configs: list[WebhookConfig]) -> None:
        self._configs = configs
        self._client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=20),
        )
        self._seq = 0

    async def close(self) -> None:
        await self._client.aclose()

    def _next_id(self) -> str:
        self._seq += 1
        return f"evt_{int(time.time())}_{self._seq}"

    def dispatch(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Non-blocking: schedule delivery in background tasks."""
        if not self._configs:
            return
        payload = {
            "event_id": self._next_id(),
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        for cfg in self._configs:
            interested = not cfg.events or event_type in cfg.events
            if not interested:
                continue
            asyncio.create_task(
                _deliver(self._client, cfg, event_type, payload),
                name=f"webhook-{event_type}",
            )
