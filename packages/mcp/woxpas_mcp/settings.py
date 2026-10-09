"""MCP-server settings — env-var driven, fail-closed by default."""

from __future__ import annotations

from uuid import UUID

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    """Configuration for the woxpas-mcp-server process.

    The server is a stdio child spawned by an agent (Claude Desktop,
    Cursor, etc.). Tenant identity is bound at startup via env so
    the agent's MCP config block (which carries the env vars) is the
    authority on which tenant this server speaks for.
    """
    model_config = SettingsConfigDict(
        env_prefix="WOXPAS_MCP_",
        extra="ignore",
    )

    tenant_id: UUID
    """REQUIRED. The tenant this MCP server speaks for. Server fails
    closed at startup if absent — there is no implicit-tenant fallback
    that could leak across agents.

    Pillar 3 (security): tenant identity is bound to the process at
    spawn time and never derived from agent input. RLS at the DB layer
    is the second-line defense; this is the first."""

    transport: str = "stdio"
    """stdio (default) | http. stdio = Claude Desktop / Cursor local agents.
    http = bind an HTTP endpoint for remote agents (Claude Mobile, web-based
    MCP clients). The http transport should sit behind a reverse proxy that
    terminates TLS."""

    http_host: str = "127.0.0.1"
    """Bind address when transport=http. Keep 127.0.0.1 if behind a reverse
    proxy; set 0.0.0.0 only with JWT + TLS enforced externally."""

    http_port: int = 7801
    """Port when transport=http."""

    http_mode: str = "streamable"
    """streamable (default, MCP 2025-03-26+) | sse (legacy). streamable uses
    a single /mcp endpoint. sse uses separate /sse + /messages endpoints."""

    auth_mode: str = "env"
    """env (default) | jwt. env = single-tenant, WOXPAS_MCP_TENANT_ID bound
    at startup. jwt = multi-tenant, resolve tenant from JWT Bearer token's
    'sub' claim per request. Requires WOXPAS_JWT_PUBLIC_KEY, _AUDIENCE,
    _ISSUER to be set."""

    jwt_public_key: str = ""
    """PEM public key for RS256/ES256 JWT verification (auth_mode=jwt)."""

    jwt_audience: str = "woxpas"
    """Expected 'aud' claim in JWT."""

    jwt_issuer: str = ""
    """Expected 'iss' claim in JWT (your auth provider URL)."""

    log_level: str = "INFO"
    """structlog level — DEBUG | INFO | WARNING | ERROR. stdout is
    reserved for MCP JSON-RPC; logs go to stderr."""

    rate_limit_per_minute: int = 60
    """Per-server-process rate limit. Token-bucket, ENFORCED before every
    tool call (see middleware.RateLimiter, wired in server.py). Per-process
    only; a shared/distributed limiter (Redis) is a v0.2 seam."""

    audit_level: str = "full"
    """full | errors_only | off. ENFORCED — every tool call is written to
    audit_log via middleware.AuditWriter (wired in server.py). 'off'
    short-circuits before any write."""

    @field_validator("audit_level")
    @classmethod
    def _audit_level_ok(cls, v: str) -> str:
        if v not in {"full", "errors_only", "off"}:
            raise ValueError(f"audit_level must be one of full|errors_only|off (got {v!r})")
        return v
