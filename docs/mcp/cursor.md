# Cursor config

Add to Cursor's MCP config (`~/.cursor/mcp.json` on macOS, or via
Settings → MCP):

```json
{
  "mcpServers": {
    "woxpas": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absolute/path/to/woxpas-memory/packages/mcp",
        "woxpas-mcp-server"
      ],
      "env": {
        "WOXPAS_MCP_TENANT_ID": "your-tenant-uuid-here",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "woxpas",
        "POSTGRES_USER": "woxpas",
        "POSTGRES_PASSWORD": "your-pg-password",
        "QDRANT_URL": "http://127.0.0.1:6334",
        "QDRANT_API_KEY": "your-qdrant-key",
        "MINIO_ENDPOINT": "127.0.0.1:9000",
        "MINIO_ACCESS_KEY": "minioadmin",
        "MINIO_SECRET_PASSWORD": "your-minio-password",
        "MINIO_SECURE": "false",
        "WOXPAS_DEV_TENANT_ID_DEFAULT": "your-tenant-uuid-here",
        "WOXPAS_DEV_DEK_HEX": "your-dek-hex-from-init-tenant"
      }
    }
  }
}
```

Reload Cursor's MCP servers via the command palette (`MCP: Reload`).

## Common workflows

**Capture-then-recall** (typical agent flow):
1. `memory_ingest` with `suggested_scope_slugs` — best practice per the
   three-track scope model.
2. Wait ~3-5s for the pipeline.
3. `memory_recall` with `intent_hint='semantic'` to verify it landed.

**Conflict resolution**:
1. `memory_list_conflicts` to see open ones.
2. `memory_get_conflict` for full detail + suggested shapes.
3. `memory_resolve_conflict` with one of `pick`/`coexistence`/`progression`/`dismissed`.

**Cross-agent prepared actions**:
1. Cursor pulls memory context via `memory_recall`.
2. Drafts an action based on its own connectors.
3. Posts via `resurface_post_prepared_action`.
4. User can switch to Claude Desktop, run `resurface_list_prepared_actions`,
   approve via `resurface_approve_prepared_action` — same data.
