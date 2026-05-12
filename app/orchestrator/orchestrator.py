"""Orchestration layer: auth → rate limiting → guardrails → redaction → upstream → audit."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.audit.store import AuditStore
from app.guardrails.safety import GuardrailViolation, SafetyFilter
from app.middleware.auth import AgentPrincipal, Authorizer, AuthorizationError
from app.middleware.ratelimit import RateLimitExceeded, RateLimiter
from app.middleware.redaction import Redactor
from app.models.mcp import ToolCallResult, ToolManifest
from app.observability.logging import get_logger, request_id_ctx
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
        rate_limiter: RateLimiter | None = None,
        audit: AuditStore | None = None,
    ) -> None:
        self.registry = registry
        self.authorizer = authorizer
        self.redactor = redactor
        self.safety = safety
        self.rate_limiter = rate_limiter
        self.audit = audit

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
            prefix = entry.server.tool_prefix or entry.server.id
            raw = name[len(prefix) + 1:] if name.startswith(prefix + ".") else name
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
        request_id = request_id_ctx.get() or "unknown"

        # Shared audit fields — updated as we progress.
        _audit = dict(
            request_id=request_id,
            agent_id=principal.agent_id,
            server_id=entry.server.id,
            tool_name=raw_tool,
            qualified_name=qualified,
            mutable=mutable,
            arguments=arguments,
            status="ok",
            is_error=False,
            error_detail=None,
            latency_ms=0.0,
            guardrail_blocked=False,
            auth_denied=False,
            rate_limited=False,
            redacted=False,
        )

        def _emit_audit(**overrides: Any) -> None:
            if self.audit is not None:
                self.audit.record(**{**_audit, **overrides})

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
            _emit_audit(status="auth_denied", is_error=True, error_detail=str(exc), auth_denied=True)
            raise OrchestrationError(str(exc), code=403) from exc

        # 2) Rate limiting
        if self.rate_limiter is not None:
            try:
                await self.rate_limiter.check(principal.agent_id, qualified)
            except RateLimitExceeded as exc:
                TOOL_CALLS_TOTAL.labels(
                    server_id=entry.server.id, tool=raw_tool, status="rate_limited"
                ).inc()
                _emit_audit(status="rate_limited", is_error=True, error_detail=str(exc), rate_limited=True)
                raise OrchestrationError(str(exc), code=429) from exc

        # 3) Guardrails
        try:
            self.safety.check(raw_tool, arguments)
        except GuardrailViolation as exc:
            TOOL_CALLS_TOTAL.labels(
                server_id=entry.server.id, tool=raw_tool, status="guardrail_blocked"
            ).inc()
            _emit_audit(status="guardrail_blocked", is_error=True, error_detail=str(exc), guardrail_blocked=True)
            raise OrchestrationError(str(exc), code=422) from exc

        # 4) Redact request
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
            _emit_audit(status="upstream_error", is_error=True, error_detail=str(exc), latency_ms=round(elapsed * 1000, 2))
            raise OrchestrationError(f"upstream error: {exc}", code=502) from exc
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - start
            TOOL_CALL_LATENCY.labels(server_id=entry.server.id, tool=raw_tool).observe(elapsed)
            TOOL_CALLS_TOTAL.labels(
                server_id=entry.server.id, tool=raw_tool, status="error"
            ).inc()
            log.exception("tool.call.error", server_id=entry.server.id, tool=raw_tool)
            _emit_audit(status="error", is_error=True, error_detail=str(exc), latency_ms=round(elapsed * 1000, 2))
            raise OrchestrationError(str(exc), code=500) from exc

        elapsed = time.perf_counter() - start
        TOOL_CALL_LATENCY.labels(server_id=entry.server.id, tool=raw_tool).observe(elapsed)
        TOOL_CALLS_TOTAL.labels(server_id=entry.server.id, tool=raw_tool, status="ok").inc()

        # 5) Redact response
        clean, resp_redacted = self.redactor.redact(raw_result, direction="response")
        was_redacted = req_redacted or resp_redacted

        log.info(
            "tool.call.ok",
            server_id=entry.server.id,
            tool=raw_tool,
            latency_ms=round(elapsed * 1000, 2),
            response_redacted=resp_redacted,
        )

        _emit_audit(
            status="ok",
            latency_ms=round(elapsed * 1000, 2),
            redacted=was_redacted,
        )

        return ToolCallResult(
            server_id=entry.server.id,
            tool=raw_tool,
            content=clean,
            is_error=False,
            latency_ms=round(elapsed * 1000, 2),
            redacted=was_redacted,
        )
