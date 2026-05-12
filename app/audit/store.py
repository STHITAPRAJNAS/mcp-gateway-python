"""Async audit log store backed by SQLAlchemy (SQLite default, Postgres via URL).

Writes are fire-and-forget via an asyncio queue so the hot path is never
blocked by DB latency. A background consumer drains the queue in batches.

Usage:
    store = AuditStore("sqlite+aiosqlite:///audit.db")
    await store.start()
    await store.record(...)
    await store.stop()
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit.models import AuditEntry, Base
from app.observability.logging import get_logger

log = get_logger("audit")

_BATCH = 50
_FLUSH_INTERVAL = 2.0  # seconds


def _hash_args(arguments: dict[str, Any]) -> str:
    raw = json.dumps(arguments, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class AuditStore:
    def __init__(self, database_url: str = "sqlite+aiosqlite:///audit.db") -> None:
        self._url = database_url
        self._engine = create_async_engine(database_url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        self._queue: asyncio.Queue[AuditEntry] = asyncio.Queue(maxsize=10_000)
        self._consumer_task: asyncio.Task | None = None

    async def start(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._consumer_task = asyncio.create_task(self._consumer(), name="audit-consumer")
        log.info("audit.store.started", url=self._url)

    async def stop(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        await self._flush()
        await self._engine.dispose()
        log.info("audit.store.stopped")

    def record(
        self,
        *,
        request_id: str,
        agent_id: str,
        server_id: str,
        tool_name: str,
        qualified_name: str,
        mutable: bool,
        status: str,
        is_error: bool = False,
        error_detail: str | None = None,
        latency_ms: float = 0.0,
        guardrail_blocked: bool = False,
        auth_denied: bool = False,
        rate_limited: bool = False,
        redacted: bool = False,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Non-blocking enqueue. Drops if queue is full (back-pressure)."""
        entry = AuditEntry(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            request_id=request_id,
            agent_id=agent_id,
            server_id=server_id,
            tool_name=tool_name,
            qualified_name=qualified_name,
            mutable=mutable,
            status=status,
            is_error=is_error,
            error_detail=error_detail,
            latency_ms=latency_ms,
            guardrail_blocked=guardrail_blocked,
            auth_denied=auth_denied,
            rate_limited=rate_limited,
            redacted=redacted,
            arguments_hash=_hash_args(arguments or {}),
        )
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            log.warning("audit.queue.full", dropped=entry.id)

    async def _consumer(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL)
            await self._flush()

    async def _flush(self) -> None:
        batch: list[AuditEntry] = []
        while not self._queue.empty() and len(batch) < _BATCH:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        try:
            async with self._session_factory() as session:
                session.add_all(batch)
                await session.commit()
            log.debug("audit.flush", count=len(batch))
        except Exception as exc:  # noqa: BLE001
            log.error("audit.flush.error", error=str(exc))

    async def query(
        self,
        *,
        agent_id: str | None = None,
        server_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditEntry).order_by(desc(AuditEntry.timestamp))
        if agent_id:
            stmt = stmt.where(AuditEntry.agent_id == agent_id)
        if server_id:
            stmt = stmt.where(AuditEntry.server_id == server_id)
        if tool_name:
            stmt = stmt.where(AuditEntry.tool_name == tool_name)
        if status:
            stmt = stmt.where(AuditEntry.status == status)
        stmt = stmt.limit(limit).offset(offset)
        async with self._session_factory() as session:
            rows = await session.execute(stmt)
            return [
                {
                    c.key: getattr(r, c.key)
                    for c in r.__table__.columns
                }
                for r in rows.scalars()
            ]
