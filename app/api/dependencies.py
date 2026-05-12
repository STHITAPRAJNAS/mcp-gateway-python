"""FastAPI dependency injection helpers."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.middleware.auth import AgentPrincipal, AuthorizationError, Authorizer
from app.orchestrator.orchestrator import Orchestrator
from app.registry.registry import ServerRegistry


def get_registry(request: Request) -> ServerRegistry:
    return request.app.state.registry


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


def get_authorizer(request: Request) -> Authorizer:
    return request.app.state.authorizer


def require_api_key(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    if settings.api_key is None:
        return
    provided = request.headers.get(settings.api_key_header)
    if provided != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key"
        )


def get_principal(
    request: Request,
    settings: Settings = Depends(get_settings),
    authorizer: Authorizer = Depends(get_authorizer),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> AgentPrincipal:
    agent_id = x_agent_id or request.headers.get(settings.agent_id_header)
    try:
        return authorizer.identify(agent_id)
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
