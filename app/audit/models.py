"""SQLAlchemy ORM model for the immutable audit log."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditEntry(Base):
    """Append-only audit record for every tool invocation attempt.

    This table must never be updated or deleted by application code.
    In production, apply a database-level row-level security policy or a
    separate write-once role to enforce immutability at the DB layer.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_agent_id", "agent_id"),
        Index("ix_audit_server_id", "server_id"),
        Index("ix_audit_tool_name", "tool_name"),
        Index("ix_audit_timestamp", "timestamp"),
        Index("ix_audit_request_id", "request_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    timestamp: Mapped[datetime] = mapped_column(index=False)
    request_id: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(256))

    # Upstream routing
    server_id: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(256))
    qualified_name: Mapped[str] = mapped_column(String(512))
    mutable: Mapped[bool] = mapped_column(Boolean, default=False)

    # Outcome
    status: Mapped[str] = mapped_column(String(32))  # ok | error | blocked | rate_limited
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    # Governance
    guardrail_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Argument fingerprint — never store raw arguments (PII risk).
    arguments_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
