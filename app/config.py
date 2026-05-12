"""Gateway configuration loaded from env + YAML."""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UpstreamServer(BaseModel):
    """A registered upstream fast-mcp server."""

    id: str = Field(..., description="Stable unique server identifier")
    name: str
    base_url: HttpUrl
    transport: Literal["http", "sse", "streamable-http"] = "http"
    auth_header: str | None = None
    auth_token: str | None = None
    timeout_seconds: float = 30.0
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    tool_prefix: str | None = None
    mutable_tools: list[str] = Field(default_factory=list)


class OIDCPolicy(BaseModel):
    """JWT / OIDC authentication policy."""

    enabled: bool = False
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_url: str | None = None
    issuer: str | None = None
    audience: str | None = None
    secret: str | None = None
    agent_id_claim: str = "sub"
    jwks_cache_ttl: float = 300.0


class AuthPolicy(BaseModel):
    """Authorization policy (ACL + OIDC)."""

    enabled: bool = True
    oidc: OIDCPolicy = OIDCPolicy()
    agent_permissions: dict[str, list[str]] = Field(default_factory=dict)
    mutable_allowed_agents: list[str] = Field(default_factory=list)


class RedactionPolicy(BaseModel):
    enabled: bool = True
    entities: list[str] = Field(
        default_factory=lambda: [
            "EMAIL_ADDRESS",
            "PHONE_NUMBER",
            "CREDIT_CARD",
            "US_SSN",
            "IP_ADDRESS",
            "IBAN_CODE",
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


class GatewayConfig(BaseModel):
    upstreams: list[UpstreamServer] = Field(default_factory=list)
    auth: AuthPolicy = AuthPolicy()
    redaction: RedactionPolicy = RedactionPolicy()
    guardrails: GuardrailsPolicy = GuardrailsPolicy()
    rate_limiting: RateLimitingPolicy = RateLimitingPolicy()
    circuit_breaker: CircuitBreakerPolicy = CircuitBreakerPolicy()

    @field_validator("upstreams")
    @classmethod
    def _unique_ids(cls, v: list[UpstreamServer]) -> list[UpstreamServer]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("upstream server ids must be unique")
        return v


class Settings(BaseSettings):
    """Runtime settings."""

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
    # Audit log database URL (aiosqlite for SQLite, asyncpg for Postgres)
    audit_db_url: str = "sqlite+aiosqlite:///audit.db"
    # Background health sweep interval (seconds, 0 = disabled)
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
