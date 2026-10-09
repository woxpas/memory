# Claude Desktop config

Add Woxpas to Claude Desktop's MCP config file.

**Config location:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "woxpas": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/woxpas-memory",
        "woxpas-mcp-server"
      ],
      "env": {
        "WOXPAS_MCP_TENANT_ID": "<your-tenant-uuid>",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "woxpas",
        "POSTGRES_USER": "woxpas",
        "POSTGRES_PASSWORD": "<from .env>",
        "QDRANT_HOST": "127.0.0.1",
        "QDRANT_PORT": "6333",
        "QDRANT_GRPC_PORT": "6334",
        "QDRANT_API_KEY": "<from .env>",
        "MINIO_ENDPOINT": "127.0.0.1:9000",
        "MINIO_ROOT_USER": "woxpas",
        "MINIO_ROOT_PASSWORD": "<from .env>",
        "WOXPAS_DEV_TENANT_ID_DEFAULT": "<your-tenant-uuid>",
        "WOXPAS_DEV_DEK_HEX": "<from .env>"
      }
    }
  }
}
```

**Fill in the placeholders** with values from your `.env` file:
- `<your-tenant-uuid>` → `WOXPAS_DEV_TENANT_ID_DEFAULT`
- `<from .env>` → the corresponding `POSTGRES_PASSWORD`, `QDRANT_API_KEY`,
  `MINIO_ROOT_PASSWORD`, `WOXPAS_DEV_DEK_HEX`

**Why this config holds secrets in plaintext:** MCP passes environment
variables to the child process as part of the agent's config block. This
file is local to your machine — keep restrictive permissions on it
(`chmod 600`). The boot daemon (`woxpas-cli unlock`) eliminates the DEK
from this config; see the boot daemon section in GETTING_STARTED.md.

---

## Quick setup (copy-paste from bootstrap output)

Run `woxpas-cli bootstrap` — at the end it prints the exact env vars
to copy. Or generate the config programmatically:

```bash
source .env
cat <<EOF
{
  "mcpServers": {
    "woxpas": {
      "command": "uv",
      "args": ["run", "--directory", "$(pwd)", "woxpas-mcp-server"],
      "env": {
        "WOXPAS_MCP_TENANT_ID": "$WOXPAS_DEV_TENANT_ID_DEFAULT",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "woxpas",
        "POSTGRES_USER": "woxpas",
        "POSTGRES_PASSWORD": "$POSTGRES_PASSWORD",
        "QDRANT_HOST": "127.0.0.1",
        "QDRANT_PORT": "6333",
        "QDRANT_GRPC_PORT": "6334",
        "QDRANT_API_KEY": "$QDRANT_API_KEY",
        "MINIO_ENDPOINT": "127.0.0.1:9000",
        "MINIO_ROOT_USER": "woxpas",
        "MINIO_ROOT_PASSWORD": "$MINIO_ROOT_PASSWORD",
        "WOXPAS_DEV_TENANT_ID_DEFAULT": "$WOXPAS_DEV_TENANT_ID_DEFAULT",
        "WOXPAS_DEV_DEK_HEX": "$WOXPAS_DEV_DEK_HEX"
      }
    }
  }
}
EOF
```

---

## Verifying

1. **Restart Claude Desktop** after editing the config.
2. Check Claude Desktop logs (**Help → Show Logs**) for `woxpas-mcp-server`
   startup messages.
3. In a new chat, ask: *"What tools do you have for Woxpas?"*
4. Try: *"Use memory_recall to search for anything I've stored."*

---

## Troubleshooting

| symptom | fix |
|---|---|
| Server doesn't appear | Verify `WOXPAS_MCP_TENANT_ID` is set. Server fails closed without it. |
| `connection refused` | Postgres / Qdrant / MinIO must be running — `docker compose ps` |
| `cloud_policy_denied` | Tenant has `allow_cloud_for_personal_data=false`. Run `woxpas-cli set-cloud-policy` |
| Slow first call | Embedder warms at MCP server startup (~3s). First recall is fast after that. |
| `DEK unwrap failed` | Wrong `WOXPAS_DEV_DEK_HEX`. Check .env — it must match the tenant. |
| Server logs not visible | Check stderr in Claude Desktop logs. MCP server logs to stderr (stdout = protocol). |
