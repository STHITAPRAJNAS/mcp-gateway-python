# MCP Gateway — Enterprise Control Plane for fast-mcp servers

A Python control plane that fronts many [fast-mcp](https://github.com/jlowin/fastmcp)
servers behind a single, policy-enforced FastAPI endpoint. Inspired by the
multi-server orchestration model popularised by projects like
[obot](https://github.com/obot-platform/obot), this gateway is designed for
enterprise deployments where you need a *single* MCP surface to expose to your
LLMs while keeping security, governance, and observability tightly controlled.

## Why a gateway?

In any non-trivial AI platform you quickly end up with several MCP servers
(database, GitHub, search, internal APIs, …). Pointing every agent at every
server directly creates a fan-out of credentials, audit gaps, missing
guardrails, and inconsistent observability. The gateway centralises:

| Concern | Where it lives |
| --- | --- |
| Discovery & registration of upstreams | `app/registry/` |
| Tool aggregation into a single manifest | `app/orchestrator/` |
| PII redaction (Presidio + regex fallback) | `app/middleware/redaction.py` |
| Mock authorization / agent ACLs | `app/middleware/auth.py` |
| Declarative safety rules (SQL keywords, numeric ranges) | `app/guardrails/safety.py` |
| Structured logs + Prometheus metrics | `app/observability/` |
| FastAPI REST + SSE interface | `app/api/` |
| Async JSON-RPC transport with retries | `app/transport/mcp_client.py` |

## Architecture

```
┌─────────┐    HTTP/SSE    ┌────────────────────────────────────────────┐
│  Agent  │ ─────────────▶ │              MCP Gateway                   │
└─────────┘                │  ┌──────────────────────────────────────┐  │
                           │  │ RequestContext + Metrics middleware  │  │
                           │  ├──────────────────────────────────────┤  │
                           │  │ Authorizer → Guardrails → Redactor   │  │
                           │  ├──────────────────────────────────────┤  │
                           │  │ Orchestrator (routing + aggregation) │  │
                           │  ├──────────────────────────────────────┤  │
                           │  │ Registry  ⇆ MCPClient (httpx async)  │  │
                           │  └──────────────────────────────────────┘  │
                           └──────┬─────────────┬───────────────┬───────┘
                                  ▼             ▼               ▼
                            ┌──────────┐  ┌──────────┐  ┌──────────────┐
                            │ pg MCP   │  │ gh MCP   │  │ search MCP   │
                            └──────────┘  └──────────┘  └──────────────┘
```

## Quickstart

```bash
pip install -e ".[dev]"
cp config/gateway.yaml config/gateway.local.yaml   # edit upstreams
MCP_GATEWAY_CONFIG_PATH=config/gateway.local.yaml mcp-gateway
```

The service listens on `http://localhost:8080` by default.

### Docker

```bash
docker compose -f docker/docker-compose.yml up --build
```

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Aggregate health (per-upstream status) |
| `GET` | `/metrics` | Prometheus exposition |
| `GET` | `/v1/registry/servers` | List registered upstreams |
| `POST` | `/v1/registry/servers` | Register a new upstream (body: `UpstreamServer`) |
| `DELETE` | `/v1/registry/servers/{id}` | Deregister |
| `POST` | `/v1/registry/servers/{id}/sync` | Re-pull tool manifest |
| `POST` | `/v1/registry/servers/{id}/enabled?enabled=true|false` | Toggle |
| `GET` | `/v1/tools?tag=...` | Aggregated tool manifest |
| `POST` | `/v1/tools/call` | Invoke a tool (`{ name, arguments, server_id? }`) |
| `POST` | `/v1/tools/call/stream` | SSE stream: `start`, `progress`, `result`/`error` |

All `/v1/*` endpoints require headers:
* `X-Agent-Id: <agent>` — identifies the calling agent for ACL + audit.
* `X-API-Key: <key>` — when `MCP_GATEWAY_API_KEY` is configured.

## Configuration (`config/gateway.yaml`)

```yaml
upstreams:
  - id: pg
    name: Postgres MCP
    base_url: http://postgres-mcp:8000
    tags: [data, sql]
    mutable_tools: [execute_sql]

auth:
  enabled: true
  mutable_allowed_agents: [ops-bot]
  agent_permissions:
    readonly-agent: ["pg.list_tables", "search.*"]

redaction:
  enabled: true
  entities: [EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD]

guardrails:
  enabled: true
  rules:
    - name: no-destructive-sql
      tool: execute_sql
      forbidden_substrings: ["DROP TABLE", "TRUNCATE"]
    - name: bounded-pagination
      tool: "*"
      numeric_ranges:
        limit: { min: 1, max: 1000 }
```

## Observability

Every tool call produces:

* a structured log line (`tool.call.start`, `tool.call.ok`, `tool.call.upstream_error`) with `request_id`, `agent_id`, `server_id`, `tool`, and latency,
* metrics:
  * `mcp_gateway_tool_calls_total{server_id,tool,status}`
  * `mcp_gateway_tool_call_latency_seconds{server_id,tool}` (histogram)
  * `mcp_gateway_guardrail_blocks_total{rule,tool}`
  * `mcp_gateway_redactions_total{direction,entity}`
  * `mcp_gateway_auth_denials_total{reason}`
  * `mcp_gateway_http_requests_total{method,path,status}`

## Tests

```bash
pytest -q
```

Tests inject an in-memory `FakeClient` so no real upstream MCP servers are
required.

## Roadmap

* OPA/Cedar policy engine instead of mock ACL.
* mTLS between gateway and upstreams.
* Token-bucket rate limiting per agent.
* OpenTelemetry tracing (replace ad-hoc logs with spans).
* Hot reload of `gateway.yaml` via SIGHUP.

## License

Apache-2.0
