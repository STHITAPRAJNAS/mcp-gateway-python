"""Mock authorization layer.

This implements a deliberately simple, swappable authorizer:
  * An agent is identified by an `X-Agent-Id` header.
  * Mutable tools require the agent to be listed in `auth.mutable_allowed_agents`.
  * Optional per-agent allow-lists let policies further restrict which tools an
    agent may invoke.

In production this would be replaced with a JWT / OIDC verifier and an
OPA / Cedar policy decision point — the shape of `authorize_tool_call` is the
extension seam.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import AuthPolicy
from app.observability.logging import get_logger
from app.observability.metrics import AUTH_DENIALS_TOTAL

log = get_logger("auth")


class AuthorizationError(Exception):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: str
    permissions: tuple[str, ...] = ()

    def can(self, action: str) -> bool:
        return "*" in self.permissions or action in self.permissions


class Authorizer:
    def __init__(self, policy: AuthPolicy) -> None:
        self.policy = policy

    def identify(self, agent_id: str | None) -> AgentPrincipal:
        if not self.policy.enabled:
            return AgentPrincipal(agent_id=agent_id or "anonymous", permissions=("*",))
        if not agent_id:
            AUTH_DENIALS_TOTAL.labels(reason="missing_agent_id").inc()
            raise AuthorizationError("agent id required", reason="missing_agent_id")
        perms = tuple(self.policy.agent_permissions.get(agent_id, []))
        return AgentPrincipal(agent_id=agent_id, permissions=perms)

    def authorize_tool_call(
        self, principal: AgentPrincipal, *, tool_name: str, qualified_name: str, mutable: bool
    ) -> None:
        if not self.policy.enabled:
            return
        # Mutable tools require explicit allow-listing.
        if mutable and principal.agent_id not in self.policy.mutable_allowed_agents:
            AUTH_DENIALS_TOTAL.labels(reason="mutable_forbidden").inc()
            log.warning(
                "auth.deny.mutable",
                agent_id=principal.agent_id,
                tool=qualified_name,
            )
            raise AuthorizationError(
                f"agent '{principal.agent_id}' may not invoke mutable tool '{qualified_name}'",
                reason="mutable_forbidden",
            )
        # Per-agent ACL: if any permissions are configured for this agent and "*"
        # is not present, require an explicit match against tool or qualified name.
        if principal.permissions and "*" not in principal.permissions:
            if not (principal.can(tool_name) or principal.can(qualified_name)):
                AUTH_DENIALS_TOTAL.labels(reason="acl_denied").inc()
                log.warning(
                    "auth.deny.acl",
                    agent_id=principal.agent_id,
                    tool=qualified_name,
                )
                raise AuthorizationError(
                    f"agent '{principal.agent_id}' lacks permission for '{qualified_name}'",
                    reason="acl_denied",
                )
