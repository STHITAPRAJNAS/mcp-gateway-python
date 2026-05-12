"""JWT / OIDC authentication.

Supports two modes, configurable via `auth.oidc`:

  HS256 (symmetric)
    - Validates tokens signed with a shared secret.
    - Good for internal service-to-service calls.

  RS256 / RS384 / RS512 (asymmetric — JWKS)
    - Downloads the provider's JWKS endpoint and caches keys with a TTL.
    - Works with Keycloak, Okta, Entra ID, Auth0, etc.
    - Falls back to re-fetching JWKS on unknown `kid`.

In both cases the `agent_id` is extracted from the JWT claim named by
`agent_id_claim` (default: "sub").

When OIDC is disabled, the gateway falls back to the X-Agent-Id header
(original mock behaviour).

Config shape:
  auth:
    oidc:
      enabled: true
      algorithms: ["RS256"]
      jwks_url: "https://idp.example.com/.well-known/jwks.json"
      issuer: "https://idp.example.com"
      audience: "mcp-gateway"
      # OR for HS256:
      secret: "supersecret"
      agent_id_claim: "sub"
      jwks_cache_ttl: 300   # seconds
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from jose import JWTError, jwk, jwt
from jose.exceptions import JWKError

from app.observability.logging import get_logger

log = get_logger("oidc")


class OIDCError(Exception):
    pass


class JWKSCache:
    """Fetches and caches a JWKS document."""

    def __init__(self, url: str, ttl: float = 300.0) -> None:
        self.url = url
        self.ttl = ttl
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0

    def _expired(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self.ttl

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            doc = resp.json()
        self._keys = {k["kid"]: k for k in doc.get("keys", []) if "kid" in k}
        self._fetched_at = time.monotonic()
        log.info("oidc.jwks.refreshed", key_count=len(self._keys))

    async def get_key(self, kid: str | None) -> Any:
        if self._expired() or (kid and kid not in self._keys):
            await self._fetch()
        if kid:
            raw = self._keys.get(kid)
            if raw is None:
                raise OIDCError(f"unknown kid '{kid}'")
            try:
                return jwk.construct(raw)
            except JWKError as exc:
                raise OIDCError(f"bad JWK: {exc}") from exc
        # No kid — return all keys; caller tries each.
        try:
            return [jwk.construct(k) for k in self._keys.values()]
        except JWKError as exc:
            raise OIDCError(f"bad JWK: {exc}") from exc


class OIDCValidator:
    """Validates a Bearer JWT and extracts the agent principal."""

    def __init__(self, config: "OIDCConfig") -> None:
        self.cfg = config
        self._jwks: JWKSCache | None = None
        if config.jwks_url:
            self._jwks = JWKSCache(config.jwks_url, ttl=config.jwks_cache_ttl)

    async def validate(self, token: str) -> dict[str, Any]:
        """Return the decoded JWT payload."""
        try:
            if self.cfg.secret:
                # HS256 — symmetric secret
                payload = jwt.decode(
                    token,
                    self.cfg.secret,
                    algorithms=self.cfg.algorithms,
                    audience=self.cfg.audience,
                    issuer=self.cfg.issuer,
                    options={"verify_aud": bool(self.cfg.audience)},
                )
                return payload

            if self._jwks is None:
                raise OIDCError("no JWKS URL or secret configured")

            # RS256 — get header kid, resolve key, decode.
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            key = await self._jwks.get_key(kid)
            keys = key if isinstance(key, list) else [key]

            last_exc: Exception | None = None
            for k in keys:
                try:
                    payload = jwt.decode(
                        token,
                        k,
                        algorithms=self.cfg.algorithms,
                        audience=self.cfg.audience,
                        issuer=self.cfg.issuer,
                        options={"verify_aud": bool(self.cfg.audience)},
                    )
                    return payload
                except JWTError as exc:
                    last_exc = exc
            raise OIDCError(f"token validation failed: {last_exc}") from last_exc

        except JWTError as exc:
            raise OIDCError(f"invalid token: {exc}") from exc

    def extract_agent_id(self, payload: dict[str, Any]) -> str:
        claim = self.cfg.agent_id_claim
        val = payload.get(claim)
        if not val:
            raise OIDCError(f"missing claim '{claim}' in token")
        return str(val)


# --- Config dataclass (referenced from app.config) ---

from dataclasses import dataclass, field  # noqa: E402 — after class defs


@dataclass
class OIDCConfig:
    enabled: bool = False
    algorithms: list[str] = field(default_factory=lambda: ["RS256"])
    jwks_url: str | None = None
    issuer: str | None = None
    audience: str | None = None
    secret: str | None = None
    agent_id_claim: str = "sub"
    jwks_cache_ttl: float = 300.0
