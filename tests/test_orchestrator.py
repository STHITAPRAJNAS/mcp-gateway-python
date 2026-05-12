import pytest


async def test_manifest_aggregates_all_servers(orchestrator):
    manifest = orchestrator.build_manifest()
    names = {t.qualified_name for t in manifest.tools}
    assert names == {"pg.list_tables", "pg.execute_sql", "search.query"}
    assert manifest.server_count == 2


async def test_manifest_tag_filter(orchestrator):
    manifest = orchestrator.build_manifest(tag="search")
    names = {t.qualified_name for t in manifest.tools}
    assert names == {"search.query"}


async def test_call_tool_routes_by_qualified_name(orchestrator):
    principal = orchestrator.authorizer.identify("ops-bot")
    result = await orchestrator.call_tool(
        principal=principal, name="pg.list_tables", arguments={}
    )
    assert result.server_id == "pg"
    assert result.tool == "list_tables"
    assert result.content == {"rows": ["users", "orders"]}


async def test_mutable_blocked_for_unauthorized_agent(orchestrator):
    principal = orchestrator.authorizer.identify("readonly")
    with pytest.raises(Exception) as exc:
        await orchestrator.call_tool(
            principal=principal,
            name="pg.execute_sql",
            arguments={"query": "SELECT 1"},
        )
    assert "mutable" in str(exc.value).lower() or "403" in str(exc.value)


async def test_guardrail_blocks_destructive_sql(orchestrator):
    principal = orchestrator.authorizer.identify("ops-bot")
    with pytest.raises(Exception):
        await orchestrator.call_tool(
            principal=principal,
            name="pg.execute_sql",
            arguments={"query": "DROP TABLE users"},
        )


async def test_response_pii_is_redacted(orchestrator):
    principal = orchestrator.authorizer.identify("ops-bot")
    result = await orchestrator.call_tool(
        principal=principal,
        name="pg.execute_sql",
        arguments={"query": "SELECT email FROM users"},
    )
    assert result.redacted is True
    assert "alice@example.com" not in str(result.content)
