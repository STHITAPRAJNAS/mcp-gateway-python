"""HTTP middlewares: request id propagation + access metrics."""
from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.logging import agent_id_ctx, get_logger, request_id_ctx
from app.observability.metrics import HTTP_REQUEST_LATENCY, HTTP_REQUESTS_TOTAL

log = get_logger("http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attaches a request id, logs access, and records Prometheus metrics."""

    async def dispatch(self, request: Request, call_next) -> Response:
        req_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        agent_id = request.headers.get("X-Agent-Id")
        req_token = request_id_ctx.set(req_id)
        agent_token = agent_id_ctx.set(agent_id)
        start = time.perf_counter()
        path = request.url.path
        method = request.method
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = req_id
            return response
        finally:
            elapsed = time.perf_counter() - start
            # Use route template when available to avoid high-cardinality labels.
            route = request.scope.get("route")
            label_path = getattr(route, "path", path) if route else path
            HTTP_REQUESTS_TOTAL.labels(
                method=method, path=label_path, status=str(status_code)
            ).inc()
            HTTP_REQUEST_LATENCY.labels(method=method, path=label_path).observe(elapsed)
            log.info(
                "http.request",
                method=method,
                path=path,
                status=status_code,
                latency_ms=round(elapsed * 1000, 2),
            )
            request_id_ctx.reset(req_token)
            agent_id_ctx.reset(agent_token)
