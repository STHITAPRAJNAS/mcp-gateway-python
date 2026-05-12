"""Tests for the async audit log store."""
import asyncio

import pytest

from app.audit.store import AuditStore


async def test_records_are_persisted(audit_store):
    audit_store.record(
        request_id="req-1",
        agent_id="ops-bot",
        server_id="pg",
        tool_name="list_tables",
        qualified_name="pg.list_tables",
        mutable=False,
        status="ok",
        latency_ms=42.5,
    )
    # Force flush.
    await audit_store._flush()
    rows = await audit_store.query(agent_id="ops-bot")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "list_tables"
    assert rows[0]["latency_ms"] == 42.5


async def test_query_filters_by_status(audit_store):
    audit_store.record(
        request_id="r1", agent_id="a", server_id="s", tool_name="t",
        qualified_name="s.t", mutable=False, status="ok",
    )
    audit_store.record(
        request_id="r2", agent_id="a", server_id="s", tool_name="t",
        qualified_name="s.t", mutable=False, status="error", is_error=True,
    )
    await audit_store._flush()
    errors = await audit_store.query(status="error")
    assert all(r["status"] == "error" for r in errors)
    assert len(errors) == 1


async def test_arguments_are_hashed_not_stored(audit_store):
    audit_store.record(
        request_id="r1", agent_id="a", server_id="s", tool_name="t",
        qualified_name="s.t", mutable=False, status="ok",
        arguments={"secret": "my-password-123"},
    )
    await audit_store._flush()
    rows = await audit_store.query()
    assert rows[0]["arguments_hash"] is not None
    # Raw argument value must never be stored.
    assert "my-password-123" not in str(rows[0])


async def test_queue_does_not_block_under_load(audit_store):
    """Enqueuing many records non-blocking should never raise."""
    for i in range(200):
        audit_store.record(
            request_id=f"r{i}", agent_id="a", server_id="s", tool_name="t",
            qualified_name="s.t", mutable=False, status="ok",
        )
