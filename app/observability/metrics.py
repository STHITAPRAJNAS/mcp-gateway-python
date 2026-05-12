"""Prometheus metrics for the gateway."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Use a dedicated registry so tests can reset cleanly.
REGISTRY = CollectorRegistry(auto_describe=True)

TOOL_CALLS_TOTAL = Counter(
    "mcp_gateway_tool_calls_total",
    "Total tool invocations routed through the gateway",
    ["server_id", "tool", "status"],
    registry=REGISTRY,
)

TOOL_CALL_LATENCY = Histogram(
    "mcp_gateway_tool_call_latency_seconds",
    "End-to-end latency for tool calls in seconds",
    ["server_id", "tool"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

HTTP_REQUESTS_TOTAL = Counter(
    "mcp_gateway_http_requests_total",
    "HTTP requests handled by the gateway",
    ["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_REQUEST_LATENCY = Histogram(
    "mcp_gateway_http_request_latency_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    registry=REGISTRY,
)

REGISTERED_SERVERS = Gauge(
    "mcp_gateway_registered_servers",
    "Currently registered upstream MCP servers",
    registry=REGISTRY,
)

REGISTERED_TOOLS = Gauge(
    "mcp_gateway_registered_tools",
    "Total tools exposed in the aggregated manifest",
    registry=REGISTRY,
)

GUARDRAIL_BLOCKS_TOTAL = Counter(
    "mcp_gateway_guardrail_blocks_total",
    "Tool calls blocked by guardrails",
    ["rule", "tool"],
    registry=REGISTRY,
)

REDACTIONS_TOTAL = Counter(
    "mcp_gateway_redactions_total",
    "PII redactions performed",
    ["direction", "entity"],
    registry=REGISTRY,
)

AUTH_DENIALS_TOTAL = Counter(
    "mcp_gateway_auth_denials_total",
    "Authorization denials",
    ["reason"],
    registry=REGISTRY,
)
