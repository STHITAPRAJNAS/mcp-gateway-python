"""Per-upstream authentication providers.

Each UpstreamServer can specify one of three auth strategies:

  static   — fixed header + token (existing behaviour, good for API keys)
  env      — token read from an environment variable at call time (supports
             external secret injection without putting secrets in YAML)
  oauth2   — client_credentials grant; token is fetched, cached, and
             refreshed automatically before expiry

Config shape per upstream:

  upstreams:
    - id: pg
      ...
      server_auth:
        strategy: static
        header: Authorization
        token: "Bearer hardcoded"       # fine for dev

    - id: gh
      ...
      server_auth:
        strategy: env
        header: Authorization
        env_var: GITHUB_MCP_TOKEN       # value: "Bearer ghp_xxx"

    - id: crm
      ...
      server_auth:
        strategy: oauth2
        header: Authorization
        oauth2_token_url: https://auth.example.com/oauth/token
        oauth2_client_id: mcp-gateway
        oauth2_client_secret_env: CRM_CLIENT_SECRET
        oauth2_scope: crm.read crm.write
        oauth2_extra_params:
          audience: crm-api

  # Per-server agent identity forwarding:
  # The gateway can forward the calling agent's id to the upstream
  # so that server-side audit logs know who triggered the call.
    - id: internal-api
      ...
      server_auth:
        strategy: static
        header: X-Internal-Key
        token: "secret"
        forward_agent_id_header: X-Forwarded-Agent
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.observability.logging import get_logger

log = get_logger("auth_provider")


class AuthProviderError(Exception):
    pass


@dataclass
class TokenCache:
    access_token: str
    expires_at: float  # monotonic


class StaticAuthProvider:
    """Returns a fixed header/token pair. Token may reference ${ENV_VAR}."""

    def __init__(self, header: str, token: str) -> None:
        self.header = header
        # Resolve simple ${VAR} env substitution at init time.
        self.token = _resolve_env(token)

    async def get_headers(self, agent_id: str | None = None) -> dict[str, str]:
        return {self.header: self.token}


class EnvVarAuthProvider:
    """Reads the token from an environment variable at call time.

    Useful when the token is injected by a secrets manager (e.g. Vault Agent
    sidecar, AWS Secrets Manager rotation) that updates the env without
    restarting the process.
    """

    def __init__(self, header: str, env_var: str) -> None:
        self.header = header
        self.env_var = env_var

    async def get_headers(self, agent_id: str | None = None) -> dict[str, str]:
        token = os.environ.get(self.env_var)
        if not token:
            raise AuthProviderError(
                f"env var '{self.env_var}' is empty or unset"
            )
        return {self.header: token}


class OAuth2ClientCredentialsProvider:
    """Fetches and caches an OAuth2 client_credentials token.

    The token is refreshed `buffer_seconds` before it expires, so callers
    always receive a valid token without needing explicit refresh calls.
    """

    def __init__(
        self,
        *,
        header: str = "Authorization",
        token_url: str,
        client_id: str,
        client_secret_env: str,
        scope: str | None = None,
        extra_params: dict[str, str] | None = None,
        buffer_seconds: float = 60.0,
    ) -> None:
        self.header = header
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret_env = client_secret_env
        self.scope = scope
        self.extra_params = extra_params or {}
        self.buffer = buffer_seconds
        self._cache: TokenCache | None = None
        self._lock = asyncio.Lock()

    def _expired(self) -> bool:
        if self._cache is None:
            return True
        return time.monotonic() >= (self._cache.expires_at - self.buffer)

    async def _fetch(self) -> TokenCache:
        secret = os.environ.get(self.client_secret_env)
        if not secret:
            raise AuthProviderError(
                f"OAuth2 client secret env var '{self.client_secret_env}' unset"
            )
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": secret,
            **self.extra_params,
        }
        if self.scope:
            data["scope"] = self.scope
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.token_url, data=data)
            resp.raise_for_status()
            body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthProviderError("OAuth2 response missing access_token")
        expires_in = float(body.get("expires_in", 3600))
        cache = TokenCache(
            access_token=token,
            expires_at=time.monotonic() + expires_in,
        )
        log.info(
            "oauth2.token.refreshed",
            token_url=self.token_url,
            expires_in=expires_in,
        )
        return cache

    async def get_headers(self, agent_id: str | None = None) -> dict[str, str]:
        async with self._lock:
            if self._expired():
                self._cache = await self._fetch()
        return {self.header: f"Bearer {self._cache.access_token}"}


# ---------- factory + config model ----------

def _resolve_env(value: str) -> str:
    """Expand ${VAR_NAME} patterns in a string."""
    import re
    def _sub(m: re.Match) -> str:
        v = os.environ.get(m.group(1), "")
        if not v:
            log.warning("auth_provider.env_var.missing", var=m.group(1))
        return v
    return re.sub(r"\$\{([^}]+)\}", _sub, value)


AuthProvider = StaticAuthProvider | EnvVarAuthProvider | OAuth2ClientCredentialsProvider


def build_auth_provider(cfg: "ServerAuthConfig") -> AuthProvider | None:
    if cfg is None:
        return None
    if cfg.strategy == "static":
        if not cfg.header or not cfg.token:
            return None
        return StaticAuthProvider(cfg.header, cfg.token)
    if cfg.strategy == "env":
        if not cfg.header or not cfg.env_var:
            return None
        return EnvVarAuthProvider(cfg.header, cfg.env_var)
    if cfg.strategy == "oauth2":
        return OAuth2ClientCredentialsProvider(
            header=cfg.header or "Authorization",
            token_url=cfg.oauth2_token_url,  # type: ignore[arg-type]
            client_id=cfg.oauth2_client_id,  # type: ignore[arg-type]
            client_secret_env=cfg.oauth2_client_secret_env,  # type: ignore[arg-type]
            scope=cfg.oauth2_scope,
            extra_params=dict(cfg.oauth2_extra_params),
            buffer_seconds=cfg.oauth2_refresh_buffer,
        )
    return None
