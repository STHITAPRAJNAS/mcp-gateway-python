import pytest

from app.config import AuthPolicy
from app.middleware.auth import AuthorizationError, Authorizer


def test_missing_agent_id_denied():
    authz = Authorizer(AuthPolicy(enabled=True))
    with pytest.raises(AuthorizationError):
        authz.identify(None)


def test_mutable_requires_allowlist():
    authz = Authorizer(
        AuthPolicy(enabled=True, mutable_allowed_agents=["ops-bot"])
    )
    p = authz.identify("readonly-agent")
    with pytest.raises(AuthorizationError) as exc:
        authz.authorize_tool_call(
            p, tool_name="execute_sql", qualified_name="pg.execute_sql", mutable=True
        )
    assert exc.value.reason == "mutable_forbidden"


def test_mutable_allowed_for_listed_agent():
    authz = Authorizer(
        AuthPolicy(
            enabled=True,
            mutable_allowed_agents=["ops-bot"],
            agent_permissions={"ops-bot": ["*"]},
        )
    )
    p = authz.identify("ops-bot")
    authz.authorize_tool_call(
        p, tool_name="execute_sql", qualified_name="pg.execute_sql", mutable=True
    )


def test_acl_restricts_per_tool():
    authz = Authorizer(
        AuthPolicy(
            enabled=True,
            agent_permissions={"readonly": ["search.query"]},
        )
    )
    p = authz.identify("readonly")
    # Allowed via qualified name
    authz.authorize_tool_call(
        p, tool_name="query", qualified_name="search.query", mutable=False
    )
    # Not allowed for an unlisted tool
    with pytest.raises(AuthorizationError):
        authz.authorize_tool_call(
            p, tool_name="list_tables", qualified_name="pg.list_tables", mutable=False
        )
