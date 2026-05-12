"""Orchestration layer.

The orchestrator is the brain of the gateway. It:
  * builds an aggregated tool manifest across all registered servers,
  * routes a tool call to the correct upstream,
  * runs the middleware chain: auth -> guardrails -> request redaction
    -> upstream call -> response redaction.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.guardrails.safety import GuardrailViolation, SafetyFilter
from app.middleware.auth import AgentPrincipal, Authorizer, AuthorizationError
from app.middleware.redaction import Redactor
from app.models.mcp import ToolCallResult, ToolManifest
from app.observability.logging import get_logger
from app.observability.metrics import TOOL_CALL_LATENCY, TOOL_CALLS_TOTAL
from app.registry.registry import RegistryEntry, ServerRegistry
from app.transport.mcp_client import UpstreamError

log = get_logger("orchestrator")


class OrchestrationError(Exception):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.code = code


class Orchestrator:
    def __init__(
        self,
        registry: ServerRegistry,
        authorizer: Authorizer,
        redactor: Redactor,
        safety: SafetyFilter,
    ) -> None:
        self.registry = registry
        self.authorizer = authorizer
        self.redactor = redactor
        self.safety = safety

    # --------- manifest ---------

    def build_manifest(self, *, tag: str | None = None) -> ToolManifest:
        tools = []
        servers = 0
        for entry in self.registry.enabled_entries():
            if not entry.healthy:
                continue
            if tag and tag not in entry.server.tags:
                continue
            servers += 1
            tools.extend(entry.tools)
        return ToolManifest(
            tools=tools,
            generated_at=datetime.now(timezone.utc).isoformat(),
            server_count=servers,
        )

    # --------- routing ---------

    def _resolve(self, name: str, server_id: str | None) -> tuple[RegistryEntry, str]:
        if server_id is not None:
            entry = self.registry.get(server_id)
            if entry is None:
                raise OrchestrationError(f"unknown server_id '{server_id}'", code=404)
            # If the caller passed the qualified name, strip the prefix.
            prefix = entry.server.tool_prefix or entry.server.id
            raw = name[len(prefix) + 1 :] if name.startswith(prefix + ".") else name
            return entry, raw
        resolved = self.registry.find_by_qualified_tool(name)
        if resolved is None:
            raise OrchestrationError(f"could not resolve tool '{name}'", code=404)
        return resolved

    # --------- execution ---------

    async def call_tool(
        self,
        *,
        principal: AgentPrincipal,
        name: str,
        arguments: dict[str, Any],
        server_id: str | None = None,
    ) -> ToolCallResult:
        entry, raw_tool = self._resolve(name, server_id)
        if not entry.server.enabled:
            raise OrchestrationError(f"server '{entry.server.id}' is disabled", code=503)

        qualified = f"{entry.server.tool_prefix or entry.server.id}.{raw_tool}"
        mutable = raw_tool in set(entry.server.mutable_tools)

        # 1) Authorization
        try:
            self.authorizer.authorize_tool_call(
                principal,
                tool_name=raw_tool,
                qualified_name=qualified,
                mutable=mutable,
            )
        except AuthorizationError as exc:
            TOOL_CALLS_TOTAL.labels(
                server_id=entry.server.id, tool=raw_tool, status="auth_denied"
            ).inc()
            raise OrchestrationError(str(exc), code=403) from exc

        # 2) Guardrails
        try:
            self.safety.check(raw_tool, arguments)
        except GuardrailViolation as exc:
            TOOL_CALLS_TOTAL.labels(
                server_id=entry.server.id, tool=raw_tool, status="guardrail_blocked"
            ).inc()
            raise OrchestrationError(str(exc), code=422) from exc

        # 3) Redact request
        redacted_args, req_redacted = self.redactor.redact(arguments, direction="request")

        log.info(
            "tool.call.start",
            server_id=entry.server.id,
            tool=raw_tool,
            qualified=qualified,
            agent_id=principal.agent_id,
            mutable=mutable,
            request_redacted=req_redacted,
        )

        start = time.perf_counter()
        try:
            raw_result = await entry.client.call_tool(raw_tool, redacted_args)
        except UpstreamError as exc:
            elapsed = time.perf_counter() - start
            TOOL_CALL_LATENCY.labels(server_id=entry.server.id, tool=raw_tool).observe(elapsed)
            TOOL_CALLS_TOTAL.labels(
                server_id=entry.server.id, tool=raw_tool, status="upstream_error"
            ).inc()
            log.warning(
                "tool.call.upstream_error",
                server_id=entry.server.id,
                tool=raw_tool,
                code=exc.code,
                error=str(exc),
            )
            raise OrchestrationError(f"upstream error: {exc}", code=502) from exc
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - start
            TOOL_CALL_LATENCY.labels(server_id=entry.server.id, tool=raw_tool).observe(elapsed)
            TOOL_CALLS_TOTAL.labels(
                server_id=entry.server.id, tool=raw_tool, status="error"
            ).inc()
            log.exception("tool.call.error", server_id=entry.server.id, tool=raw_tool)
            raise OrchestrationError(str(exc), code=500) from exc

        elapsed = time.perf_counter() - start
        TOOL_CALL_LATENCY.labels(server_id=entry.server.id, tool=raw_tool).observe(elapsed)
        TOOL_CALLS_TOTAL.labels(
            server_id=entry.server.id, tool=raw_tool, status="ok"
        ).inc()

        # 4) Redact response
        clean, resp_redacted = self.redactor.redact(raw_result, direction="response")

        log.info(
            "tool.call.ok",
            server_id=entry.server.id,
            tool=raw_tool,
            latency_ms=round(elapsed * 1000, 2),
            response_redacted=resp_redacted,
        )

        return ToolCallResult(
            server_id=entry.server.id,
            tool=raw_tool,
            content=clean,
            is_error=False,
            latency_ms=round(elapsed * 1000, 2),
            redacted=req_redacted or resp_redacted,
        )
