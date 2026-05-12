"""Structured logging configuration using structlog."""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

# Context vars propagated into every log line within an HTTP request.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
agent_id_ctx: ContextVar[str | None] = ContextVar("agent_id", default=None)


def _inject_context(_, __, event_dict: dict) -> dict:
    req_id = request_id_ctx.get()
    agent = agent_id_ctx.get()
    if req_id:
        event_dict.setdefault("request_id", req_id)
    if agent:
        event_dict.setdefault("agent_id", agent)
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Configure structlog + stdlib logging once at startup."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()
