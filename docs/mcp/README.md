# Woxpas MCP server

Stdio MCP server exposing 27 tools (13 `memory_*` + 14 `resurface_*`)
for any MCP-capable agent (Claude Desktop, Cursor, custom).

## Quick start

```bash
# Start your tenant: see scripts/migrate.sh + woxpas-cli init-tenant
# Then:
WOXPAS_MCP_TENANT_ID=<your-tenant-uuid> \
  uv run --project packages/mcp woxpas-mcp-server
```

The server speaks JSON-RPC over stdin/stdout. It refuses to start
without `WOXPAS_MCP_TENANT_ID` (Pillar 3 — tenant identity is bound at
process spawn, never derived from agent input).

## Configuration (env vars)

| var | default | meaning |
|---|---|---|
| `WOXPAS_MCP_TENANT_ID` | **required** | UUID of the tenant this server speaks for |
| `WOXPAS_MCP_LOG_LEVEL` | `INFO` | structlog level — DEBUG/INFO/WARNING/ERROR |
| `WOXPAS_MCP_RATE_LIMIT_PER_MINUTE` | `60` | token-bucket rate limit |
| `WOXPAS_MCP_AUDIT_LEVEL` | `full` | `full` / `errors_only` / `off` |

Plus the standard Woxpas env vars (POSTGRES_*, QDRANT_*, MINIO_*, model
config, WOXPAS_DEV_DEK_HEX in dev).

## Client integration

- [Claude Desktop](claude_desktop.md)
- [Cursor](cursor.md)
- [Mobile & remote access](mobile.md) — Claude Mobile, other devices

## Tool surface

See [tools.md](tools.md) for the full inventory + when to use each.

## Architecture

Single tenant per server process. Stdio transport (default) or HTTP/SSE
(via `WOXPAS_MCP_TRANSPORT=http`). All tool handlers run
in-process against shared engines (RetrievalEngine, SynthesisEngine,
ResurfaceEngine, ScopeAdminEngine, DocumentAdminEngine,
DocumentReadEngine, ConflictEngine, IngressStage).

Security:
- Tenant_id from env, never input
- Postgres RLS as second-line defense
- Cloud-policy gate honored on `memory_ask`
- Audit log row per tool call (`mcp.tool.<name>`)
- Token-bucket rate limit
- Structured error codes (`not_found`, `cloud_policy_denied`,
  `rate_limited`, `validation_failed`, `invalid_state`, …)
- No stack traces leaked to agents (stderr only)

## Smoke tests

```bash
tests/smoke/phase_5a55_mcp_full.sh   # comprehensive
tests/smoke/phase_5a51_mcp_scaffold.sh
tests/smoke/phase_5a52_memory_tools.sh
tests/smoke/phase_5a53_resurface_tools.sh
tests/smoke/phase_5a54_hardening.sh
```
