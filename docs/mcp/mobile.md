# Mobile & remote access

Woxpas defaults to local-only (stdio MCP). This guide covers accessing your
memory from any remote MCP client — mobile apps, web-based agents, another
machine on your network, or a cloud deployment.

Auth is handled by your reverse proxy (Tailscale Funnel, Cloudflare Tunnel,
nginx). The MCP server trusts the proxy — no built-in auth layer.

---

## How it works

The MCP server supports two transports:

| Mode | Transport | Protocol | Use case |
|---|---|---|---|
| `stdio` (default) | stdin/stdout | MCP 2024-11-05 | Claude Desktop, Cursor |
| `http` (streamable) | HTTP `/mcp` | MCP 2025-03-26+ | Mobile, web, remote |
| `http` (sse) | HTTP `/sse` + `/messages` | MCP 2024-11-05 (legacy) | Older clients |

```
Remote client ──HTTPS──▶ Tunnel ──HTTP──▶ woxpas-mcp-server (:7801)
              (TLS+auth)  (Tailscale/CF)  (127.0.0.1, /mcp)
```

---

## Step 1: Start the server

```bash
# Streamable HTTP (default, MCP 2025-03-26+)
uv run woxpas-cli serve-mcp

# Legacy SSE (for older MCP clients)
uv run woxpas-cli serve-mcp --http-mode sse
```

The server listens on `127.0.0.1:7801`:

| Endpoint | Method | Purpose |
|---|---|---|
| `/healthz` | GET | Health check |
| `/mcp` | GET / POST | Streamable HTTP (POST → JSON-RPC, GET → SSE) |
| `/sse` + `/messages` | GET / POST | Legacy SSE (only when `--http-mode sse`) |

---

## Step 2: Expose via a tunnel

### Tailscale Funnel (public internet)

```bash
tailscale funnel 7801
```

Your server is reachable at `https://<hostname>.<tailnet>.ts.net`.

### Tailscale Serve (tailnet only)

```bash
tailscale serve --bg 7801
```

### Cloudflare Tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:7801
```

---

## Step 3: Connect your client

Any MCP client that speaks HTTP/SSE can connect. Point it at your tunnel URL.

**Streamable HTTP (recommended):**
- URL: `https://<your-tunnel>/mcp`
- Auth: handled by your tunnel (Tailscale auth, Cloudflare Access, etc.)

**Legacy SSE:**
- URL: `https://<your-tunnel>/sse`

**Verifying it works:**

```bash
# Health check
curl https://<your-tunnel>/healthz
# → {"ok":true,"transport":"http-streamable"}
```

---

## Security

- [ ] MCP server binds to `127.0.0.1`, not `0.0.0.0`
- [ ] TLS is terminated at the tunnel/proxy
- [ ] The tunnel authenticates the client (Tailscale auth, Cloudflare Access, etc.)
- [ ] `WOXPAS_DEV_DEK_HEX` is never set in production — use the boot daemon
