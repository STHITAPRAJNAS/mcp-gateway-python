"""Gateway configuration loaded from env + YAML."""
from __future__ import annotations

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
    # Optional explicit tool-name prefix; default uses server id
    tool_prefix: str | None = None
    # Tools considered mutable for this server (e.g. write/delete operations)
    mutable_tools: list[str] = Field(default_factory=list)


class AuthPolicy(BaseModel):
    """Mock authorization policy."""

    enabled: bool = True
    # agent_id -> list of allowed actions, "*" = all
    agent_permissions: dict[str, list[str]] = Field(default_factory=dict)
    # agent_ids permitted to invoke mutable tools
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
    # Redact request arguments before forwarding, and responses before returning
    redact_requests: bool = True
    redact_responses: bool = True
    replacement: str = "<REDACTED:{entity}>"


class SafetyRule(BaseModel):
    name: str
    # Match against tool name; "*" matches any
    tool: str = "*"
    # Disallowed substrings (case-insensitive) in any string argument
    forbidden_substrings: list[str] = Field(default_factory=list)
    # Per-argument numeric ranges: {"arg_name": {"min": 0, "max": 100}}
    numeric_ranges: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Regex patterns argument values must NOT match
    forbidden_regex: list[str] = Field(default_factory=list)


class GuardrailsPolicy(BaseModel):
    enabled: bool = True
    rules: list[SafetyRule] = Field(default_factory=list)


class GatewayConfig(BaseModel):
    upstreams: list[UpstreamServer] = Field(default_factory=list)
    auth: AuthPolicy = AuthPolicy()
    redaction: RedactionPolicy = RedactionPolicy()
    guardrails: GuardrailsPolicy = GuardrailsPolicy()

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
    # JWT/auth header used to identify the calling agent
    agent_id_header: str = "X-Agent-Id"
    api_key_header: str = "X-API-Key"
    # If set, require this API key on all calls
    api_key: str | None = None

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
