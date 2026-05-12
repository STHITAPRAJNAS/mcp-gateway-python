"""Authorization layer: OIDC JWT validation + ACL enforcement.

Identification flow:
  1. If a Bearer token is present in the Authorization header and OIDC is
     enabled, validate it and extract the agent_id from the configured claim.
  2. Otherwise fall back to the X-Agent-Id header (useful for service accounts
     and development environments).

ACL enforcement is unchanged from the original design — the seam between
identity (who is this?) and authorisation (what may they do?) is clean.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import AuthPolicy
from app.middleware.oidc import OIDCConfig, OIDCError, OIDCValidator
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
        oidc_cfg = OIDCConfig(
            enabled=policy.oidc.enabled,
            algorithms=list(policy.oidc.algorithms),
            jwks_url=policy.oidc.jwks_url,
            issuer=policy.oidc.issuer,
            audience=policy.oidc.audience,
            secret=policy.oidc.secret,
            agent_id_claim=policy.oidc.agent_id_claim,
            jwks_cache_ttl=policy.oidc.jwks_cache_ttl,
        )
        self._oidc: OIDCValidator | None = (
            OIDCValidator(oidc_cfg) if oidc_cfg.enabled else None
        )

    async def identify(
        self,
        agent_id_header: str | None,
        bearer_token: str | None = None,
    ) -> AgentPrincipal:
        """Resolve a request to an AgentPrincipal."""
        if not self.policy.enabled:
            return AgentPrincipal(
                agent_id=agent_id_header or "anonymous", permissions=("*",)
            )

        # --- OIDC path ---
        if self._oidc is not None and bearer_token:
            try:
                payload = await self._oidc.validate(bearer_token)
                agent_id = self._oidc.extract_agent_id(payload)
            except OIDCError as exc:
                AUTH_DENIALS_TOTAL.labels(reason="invalid_token").inc()
                raise AuthorizationError(str(exc), reason="invalid_token") from exc
        # --- Header fallback ---
        elif agent_id_header:
            agent_id = agent_id_header
        else:
            AUTH_DENIALS_TOTAL.labels(reason="missing_agent_id").inc()
            raise AuthorizationError(
                "agent id required (X-Agent-Id header or Bearer token)",
                reason="missing_agent_id",
            )

        perms = tuple(self.policy.agent_permissions.get(agent_id, []))
        return AgentPrincipal(agent_id=agent_id, permissions=perms)

    def authorize_tool_call(
        self, principal: AgentPrincipal, *, tool_name: str, qualified_name: str, mutable: bool
    ) -> None:
        if not self.policy.enabled:
            return
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
