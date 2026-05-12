"""Tests for per-server auth providers."""
from __future__ import annotations

import os
import time

import pytest
import respx
import httpx

from app.transport.auth_provider import (
    AuthProviderError,
    EnvVarAuthProvider,
    OAuth2ClientCredentialsProvider,
    StaticAuthProvider,
    _resolve_env,
)


# ---------- StaticAuthProvider ----------

async def test_static_returns_fixed_header():
    p = StaticAuthProvider("Authorization", "Bearer token123")
    headers = await p.get_headers()
    assert headers == {"Authorization": "Bearer token123"}


async def test_static_expands_env_var(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "Bearer from-env")
    p = StaticAuthProvider("Authorization", "${MY_TOKEN}")
    headers = await p.get_headers()
    assert headers == {"Authorization": "Bearer from-env"}


# ---------- EnvVarAuthProvider ----------

async def test_env_var_reads_at_call_time(monkeypatch):
    monkeypatch.setenv("SVC_TOKEN", "Bearer first")
    p = EnvVarAuthProvider("Authorization", "SVC_TOKEN")
    h1 = await p.get_headers()

    monkeypatch.setenv("SVC_TOKEN", "Bearer rotated")
    h2 = await p.get_headers()

    assert h1["Authorization"] == "Bearer first"
    assert h2["Authorization"] == "Bearer rotated"


async def test_env_var_raises_when_unset(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    p = EnvVarAuthProvider("X-Key", "MISSING_VAR")
    with pytest.raises(AuthProviderError, match="MISSING_VAR"):
        await p.get_headers()


# ---------- OAuth2ClientCredentialsProvider ----------

@pytest.fixture
def oauth2_provider(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET", "super-secret")
    return OAuth2ClientCredentialsProvider(
        token_url="https://auth.example.com/token",
        client_id="gateway",
        client_secret_env="CLIENT_SECRET",
        scope="read write",
        buffer_seconds=10.0,
    )


@respx.mock
async def test_oauth2_fetches_token(oauth2_provider):
    respx.post("https://auth.example.com/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-abc", "expires_in": 3600}
        )
    )
    headers = await oauth2_provider.get_headers()
    assert headers == {"Authorization": "Bearer tok-abc"}


@respx.mock
async def test_oauth2_caches_token(oauth2_provider):
    route = respx.post("https://auth.example.com/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-cached", "expires_in": 3600}
        )
    )
    await oauth2_provider.get_headers()
    await oauth2_provider.get_headers()
    # Token endpoint should only be hit once (cached).
    assert route.call_count == 1


@respx.mock
async def test_oauth2_refreshes_when_expired(oauth2_provider):
    oauth2_provider.buffer_seconds = 9999  # buffer > expires_in → always expired
    route = respx.post("https://auth.example.com/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "new-tok", "expires_in": 1}
        )
    )
    await oauth2_provider.get_headers()
    await oauth2_provider.get_headers()
    assert route.call_count == 2


@respx.mock
async def test_oauth2_raises_on_missing_secret(monkeypatch):
    monkeypatch.delenv("CLIENT_SECRET", raising=False)
    p = OAuth2ClientCredentialsProvider(
        token_url="https://auth.example.com/token",
        client_id="gw",
        client_secret_env="CLIENT_SECRET",
    )
    with pytest.raises(AuthProviderError, match="CLIENT_SECRET"):
        await p.get_headers()


# ---------- forward_agent_id_header ----------

async def test_static_does_not_forward_agent_by_default():
    p = StaticAuthProvider("Authorization", "Bearer x")
    headers = await p.get_headers(agent_id="ops-bot")
    assert "X-Forwarded-Agent" not in headers


async def test_env_provider_returns_only_auth_header(monkeypatch):
    monkeypatch.setenv("TOKEN", "Bearer y")
    p = EnvVarAuthProvider("Authorization", "TOKEN")
    headers = await p.get_headers(agent_id="ops-bot")
    # Provider itself doesn't forward — MCPClient handles that separately.
    assert list(headers.keys()) == ["Authorization"]
