"""Tests for JWT/OIDC validation (HS256 path; RS256 needs a live JWKS)."""
import time

import pytest
from jose import jwt

from app.middleware.oidc import OIDCConfig, OIDCError, OIDCValidator


SECRET = "test-shared-secret"


def _make_token(sub: str = "agent-x", exp_offset: int = 300, **extra) -> str:
    payload = {"sub": sub, "iat": int(time.time()), "exp": int(time.time()) + exp_offset, **extra}
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def validator():
    return OIDCValidator(
        OIDCConfig(
            enabled=True,
            algorithms=["HS256"],
            secret=SECRET,
            agent_id_claim="sub",
        )
    )


async def test_valid_token_accepted(validator):
    token = _make_token(sub="my-agent")
    payload = await validator.validate(token)
    assert payload["sub"] == "my-agent"


async def test_extract_agent_id(validator):
    token = _make_token(sub="svc-account")
    payload = await validator.validate(token)
    agent_id = validator.extract_agent_id(payload)
    assert agent_id == "svc-account"


async def test_expired_token_rejected(validator):
    token = _make_token(exp_offset=-10)
    with pytest.raises(OIDCError, match="invalid token"):
        await validator.validate(token)


async def test_tampered_token_rejected(validator):
    token = _make_token() + "tampered"
    with pytest.raises(OIDCError):
        await validator.validate(token)


async def test_wrong_secret_rejected():
    validator = OIDCValidator(OIDCConfig(enabled=True, algorithms=["HS256"], secret="other"))
    token = _make_token()
    with pytest.raises(OIDCError):
        await validator.validate(token)


async def test_missing_claim_raises(validator):
    token = _make_token()
    payload = await validator.validate(token)
    v2 = OIDCValidator(OIDCConfig(enabled=True, algorithms=["HS256"], secret=SECRET, agent_id_claim="preferred_username"))
    with pytest.raises(OIDCError, match="missing claim"):
        v2.extract_agent_id(payload)
