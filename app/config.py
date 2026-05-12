"""Gateway configuration loaded from env + YAML."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------- per-server auth ----------

class ServerAuthConfig(BaseModel):
    """Authentication config for a single upstream MCP server.

    strategy: static   → fixed header + token (supports ${ENV_VAR} expansion)
              env      → token read from env_var at call time
              oauth2   → client_credentials grant with auto-refresh
    """

    strategy: Literal["static", "env", "oauth2"] = "static"
    header: str | None = None
    # static: literal value or "${ENV_VAR}" reference
    token: str | None = None
    # env: name of the env var that holds the full token value
    env_var: str | None = None
    # oauth2 fields
    oauth2_token_url: str | None = None
    oauth2_client_id: str | None = None
    oauth2_client_secret_env: str | None = None   # env var holding the secret
    oauth2_scope: str | None = None
    oauth2_extra_params: dict[str, str] = Field(default_factory=dict)
    oauth2_refresh_buffer: float = 60.0
    # Forward the calling agent's id to the upstream server as this header.
    # Lets downstream servers log/attribute calls to the original agent.
    forward_agent_id_header: str | None = None


# ---------- naming strategy ----------

class NamingConfig(BaseModel):
    """Controls how tool names are qualified to prevent collisions.

    strategy:
      prefix_always     — always qualify as <server_id>.<tool> (default, safe)
      prefix_on_conflict — use bare name when globally unique; prefix otherwise
      bare              — never prefix (caller's responsibility; collisions error)
    on_conflict:
      warn   — log a warning; first-registered server wins
      error  — refuse to register the second tool (raises ValueError)
      suffix — append _<server_id> to the colliding name: search_internal
    """

    strategy: Literal["prefix_always", "prefix_on_conflict", "bare"] = "prefix_always"
    on_conflict: Literal["warn", "error", "suffix"] = "warn"


# ---------- upstream server ----------

class UpstreamServer(BaseModel):
    """A registered upstream fast-mcp server."""

    id: str = Field(..., description="Stable unique server identifier")
    name: str
    base_url: HttpUrl
    transport: Literal["http", "sse", "streamable-http"] = "streamable-http"
    # Legacy single-header auth — still supported; takes precedence if set.
    # Prefer server_auth for new deployments.
    auth_header: str | None = None
    auth_token: str | None = None
    # Structured auth config (overrides auth_header/auth_token when present)
    server_auth: ServerAuthConfig | None = None
    timeout_seconds: float = 30.0
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    tool_prefix: str | None = None
    mutable_tools: list[str] = Field(default_factory=list)


# ---------- gateway-level auth (agent identity) ----------

class OIDCPolicy(BaseModel):
    enabled: bool = False
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_url: str | None = None
    issuer: str | None = None
    audience: str | None = None
    secret: str | None = None
    agent_id_claim: str = "sub"
    jwks_cache_ttl: float = 300.0


class AuthPolicy(BaseModel):
    enabled: bool = True
    oidc: OIDCPolicy = OIDCPolicy()
    agent_permissions: dict[str, list[str]] = Field(default_factory=dict)
    mutable_allowed_agents: list[str] = Field(default_factory=list)


# ---------- other policies ----------

class RedactionPolicy(BaseModel):
    enabled: bool = True
    entities: list[str] = Field(
        default_factory=lambda: [
            "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
            "US_SSN", "IP_ADDRESS", "IBAN_CODE",
        ]
    )
    score_threshold: float = 0.5
    redact_requests: bool = True
    redact_responses: bool = True
    replacement: str = "<REDACTED:{entity}>"


class SafetyRule(BaseModel):
    name: str
    tool: str = "*"
    forbidden_substrings: list[str] = Field(default_factory=list)
    numeric_ranges: dict[str, dict[str, float]] = Field(default_factory=dict)
    forbidden_regex: list[str] = Field(default_factory=list)


class GuardrailsPolicy(BaseModel):
    enabled: bool = True
    rules: list[SafetyRule] = Field(default_factory=list)


class RateLimitingPolicy(BaseModel):
    enabled: bool = True
    default_agent_rate: float = 10.0
    default_agent_burst: float = 20.0
    default_tool_rate: float = 5.0
    default_tool_burst: float = 10.0
    agents: dict[str, dict[str, float]] = Field(default_factory=dict)
    tools: dict[str, dict[str, float]] = Field(default_factory=dict)


class CircuitBreakerPolicy(BaseModel):
    enabled: bool = True
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    probe_successes: int = 2


class SchemaValidationConfig(BaseModel):
    enabled: bool = True
    strict_on_missing_schema: bool = False


class CacheConfig(BaseModel):
    enabled: bool = True
    manifest_ttl: float = 60.0
    tool_results_enabled: bool = True
    default_tool_ttl: float = 0.0
    tool_ttls: dict[str, float] = Field(default_factory=dict)


class WebhookConfig(BaseModel):
    url: str
    secret_env: str | None = None
    events: list[str] = Field(default_factory=list)
    timeout: float = 5.0
    max_retries: int = 3


# ---------- root config ----------

class GatewayConfig(BaseModel):
    upstreams: list[UpstreamServer] = Field(default_factory=list)
    auth: AuthPolicy = AuthPolicy()
    redaction: RedactionPolicy = RedactionPolicy()
    guardrails: GuardrailsPolicy = GuardrailsPolicy()
    rate_limiting: RateLimitingPolicy = RateLimitingPolicy()
    circuit_breaker: CircuitBreakerPolicy = CircuitBreakerPolicy()
    naming: NamingConfig = NamingConfig()
    schema_validation: SchemaValidationConfig = SchemaValidationConfig()
    cache: CacheConfig = CacheConfig()
    webhooks: list[WebhookConfig] = Field(default_factory=list)

    @field_validator("upstreams")
    @classmethod
    def _unique_ids(cls, v: list[UpstreamServer]) -> list[UpstreamServer]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("upstream server ids must be unique")
        return v


# ---------- runtime settings ----------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    json_logs: bool = True
    config_path: str = "config/gateway.yaml"
    metrics_path: str = "/metrics"
    request_timeout_seconds: float = 60.0
    agent_id_header: str = "X-Agent-Id"
    api_key_header: str = "X-API-Key"
    api_key: str | None = None
    audit_db_url: str = "sqlite+aiosqlite:///audit.db"
    health_sweep_interval: float = 60.0

    def load_gateway_config(self) -> GatewayConfig:
        path = Path(self.config_path)
        if not path.exists():
            return GatewayConfig()
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return GatewayConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
