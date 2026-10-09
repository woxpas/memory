"""Cross-cutting middleware for tool calls — rate limiting, audit log,
latency instrumentation. Wraps the registry handler invocation in
server.py.

v0.1 in-process implementations:
  - RateLimiter: token-bucket, per-server-process
  - AuditWriter: writes one audit_log row per call
  - latency: structlog event with tool, ms, success

v0.2 forward-design seams (documented in code, not implemented):
  - Distributed rate-limiter (Redis token bucket) for multi-process
    deployments
  - Audit log batching to reduce write amplification on high-volume
    tenants
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from woxpas_mcp.errors import RateLimited

log = structlog.get_logger(__name__)


# ─── Rate limiter (token bucket) ───────────────────────────────────────────


@dataclass(slots=True)
class _Bucket:
    tokens:    float
    last_fill: float


class RateLimiter:
    """Per-server-process token-bucket rate limiter.

    Pillars:
      Speed — sub-microsecond check; doesn't block normal traffic.
      Security — agents can't DOS the memory layer with a runaway loop.

    v0.2 (multi-process): swap this in-memory state for a Redis token
    bucket via the McpSettings adapter pattern.
    """

    def __init__(self, *, rate_per_minute: int = 60, capacity: int | None = None):
        self._rate_per_sec = rate_per_minute / 60.0
        self._capacity     = float(capacity) if capacity else float(rate_per_minute)
        self._bucket       = _Bucket(tokens=self._capacity, last_fill=time.monotonic())
        self._lock         = asyncio.Lock()

    async def acquire(self) -> None:
        """Consume one token. Raises RateLimited (with retry_after_ms in
        the message) if no tokens available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._bucket.last_fill
            self._bucket.tokens = min(
                self._capacity,
                self._bucket.tokens + elapsed * self._rate_per_sec,
            )
            self._bucket.last_fill = now
            if self._bucket.tokens >= 1.0:
                self._bucket.tokens -= 1.0
                return
            # No tokens — compute retry_after.
            need = 1.0 - self._bucket.tokens
            retry_after_sec = need / self._rate_per_sec
            raise RateLimited(
                f"rate limit exceeded ({int(self._rate_per_sec * 60)}/min); "
                f"retry_after_ms={int(retry_after_sec * 1000)}",
                retriable=True,
            )


# ─── Audit log writer ──────────────────────────────────────────────────────


class AuditWriter:
    """Writes one audit_log row per tool invocation.

    Schema (existing audit_log table):
      tenant_id, actor, actor_kind, action, target_kind?, target_id?,
      outcome ('success'|'denied'|'error'), metadata jsonb, client jsonb

    Audit_level controls volume:
      full         — every call
      errors_only  — only failures
      off          — disabled
    """

    def __init__(
        self, *, pool, level: str = "full",
        actor: str = "mcp-agent",
    ) -> None:
        self._pool   = pool
        self._level  = level
        self._actor  = actor

    async def record(
        self, *,
        tenant_id: UUID,
        tool_name: str,
        latency_ms: float,
        success:   bool,
        error_code: str | None = None,
        target_id:  UUID | None = None,
        target_kind: str | None = None,
    ) -> None:
        if self._level == "off":
            return
        if self._level == "errors_only" and success:
            return

        outcome = "success" if success else ("denied" if error_code in {"cloud_policy_denied", "rate_limited"} else "error")
        metadata: dict[str, Any] = {
            "latency_ms": round(latency_ms, 1),
            "tool":       tool_name,
        }
        if error_code:
            metadata["error_code"] = error_code

        try:
            async with self._pool.tenant_session(tenant_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_log
                      (tenant_id, actor, actor_kind, action, target_kind, target_id,
                       outcome, metadata)
                    VALUES ($1, $2, 'agent', $3, $4, $5, $6, $7::jsonb)
                    """,
                    tenant_id, self._actor,
                    f"mcp.tool.{tool_name}",
                    target_kind, target_id, outcome, metadata,
                )
        except Exception as e:
            # Never let audit-log failures break the tool call. Surface
            # via stderr only.
            log.warning("mcp.audit.write_failed",
                        tool=tool_name,
                        error_class=type(e).__name__,
                        error=str(e)[:200])


# ─── Latency instrumentation helper ────────────────────────────────────────


def emit_latency(
    *, tool: str, tenant_id: UUID, ms: float, success: bool,
    error_code: str | None = None,
) -> None:
    """Single structlog event per tool call. Goes to stderr (stdout is
    reserved for MCP protocol) so ops can pipe it to log aggregation."""
    log.info(
        "mcp.tool.invoked",
        tool=tool,
        tenant_id=str(tenant_id),
        ms=round(ms, 1),
        success=success,
        error_code=error_code,
    )
