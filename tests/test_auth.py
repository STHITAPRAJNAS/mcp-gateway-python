import pytest

from app.config import AuthPolicy, OIDCPolicy
from app.middleware.auth import AuthorizationError, Authorizer


@pytest.fixture
def authz():
    return Authorizer(AuthPolicy(enabled=True, oidc=OIDCPolicy(enabled=False)))


async def test_missing_agent_id_denied(authz):
    with pytest.raises(AuthorizationError):
        await authz.identify(None, None)


async def test_agent_id_header_accepted(authz):
    p = await authz.identify("ops-bot", None)
    assert p.agent_id == "ops-bot"


async def test_mutable_requires_allowlist():
    authz = Authorizer(
        AuthPolicy(enabled=True, oidc=OIDCPolicy(enabled=False), mutable_allowed_agents=["ops-bot"])
    )
    p = await authz.identify("readonly-agent", None)
    with pytest.raises(AuthorizationError) as exc:
        authz.authorize_tool_call(
            p, tool_name="execute_sql", qualified_name="pg.execute_sql", mutable=True
        )
    assert exc.value.reason == "mutable_forbidden"


async def test_mutable_allowed_for_listed_agent():
    authz = Authorizer(
        AuthPolicy(
            enabled=True,
            oidc=OIDCPolicy(enabled=False),
            mutable_allowed_agents=["ops-bot"],
            agent_permissions={"ops-bot": ["*"]},
        )
    )
    p = await authz.identify("ops-bot", None)
    authz.authorize_tool_call(
        p, tool_name="execute_sql", qualified_name="pg.execute_sql", mutable=True
    )


async def test_acl_restricts_per_tool():
    authz = Authorizer(
        AuthPolicy(
            enabled=True,
            oidc=OIDCPolicy(enabled=False),
            agent_permissions={"readonly": ["search.query"]},
        )
    )
    p = await authz.identify("readonly", None)
    authz.authorize_tool_call(
        p, tool_name="query", qualified_name="search.query", mutable=False
    )
    with pytest.raises(AuthorizationError):
        authz.authorize_tool_call(
            p, tool_name="list_tables", qualified_name="pg.list_tables", mutable=False
        )
