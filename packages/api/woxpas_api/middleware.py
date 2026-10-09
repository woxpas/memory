"""HTTP audit-log middleware — Phase 5a.5+ Pillar-3 hardening.

Writes one audit_log row per request that resolves a tenant. Mirrors the
MCP layer's AuditWriter so direct HTTP callers (woxpas-cli, custom
clients, browser) are audited the same way agent calls produce.

Skips:
  - /health and /healthz (high-frequency, low-value)
  - requests that didn't resolve a tenant (auth failures already logged
    via structlog at the auth layer)

Best-effort: an audit-log write failure is logged to stderr but never
fails the request (Pillar 1: speed; Pillar 3: liveness).
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

log = structlog.get_logger(__name__)

_SKIP_PATHS = {"/health", "/healthz"}


class AuditMiddleware(BaseHTTPMiddleware):
    """One audit_log row per resolved-tenant request.

    Reads tenant_id from request.state.tenant_id_for_audit (set by the
    `tenant_id_dep` dependency in auth.py). Routes that don't depend on
    that resolver (e.g. /health) skip naturally because the attribute
    is never set.

    Honors API_AUDIT_LEVEL ∈ {full, errors_only, off}.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        api_settings = request.app.state.api_settings
        level = getattr(api_settings, "audit_level", "full")
        if level == "off":
            return await call_next(request)

        t0 = time.monotonic()
        response: Response = await call_next(request)
        ms = (time.monotonic() - t0) * 1000.0

        # Tenant only resolved if a route hit tenant_id_dep. Skip otherwise.
        tenant_id: UUID | None = getattr(request.state, "tenant_id_for_audit", None)
        if tenant_id is None:
            return response

        success = response.status_code < 400
        if level == "errors_only" and success:
            return response

        outcome = (
            "success" if success
            else "denied" if response.status_code in {401, 403, 409}
            else "error"
        )
        metadata: dict[str, Any] = {
            "method":     request.method,
            "path":       request.url.path,
            "status":     response.status_code,
            "latency_ms": round(ms, 1),
        }

        # Pull pool from app state. Schema: audit_log(tenant_id, actor,
        # actor_kind, action, target_kind?, target_id?, outcome, metadata).
        try:
            app_state = getattr(request.app.state, "app", None)
            if app_state is None:
                return response
            pool = app_state.ctx.pool
            async with pool.tenant_session(tenant_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_log
                      (tenant_id, actor, actor_kind, action, outcome, metadata)
                    VALUES ($1, $2, 'agent', $3, $4, $5::jsonb)
                    """,
                    tenant_id,
                    "http-api",
                    f"http.{request.method.lower()}.{_slug(request.url.path)}",
                    outcome, metadata,
                )
        except Exception as e:
            log.warning(
                "api.audit.write_failed",
                tenant_id=str(tenant_id),
                path=request.url.path,
                error_class=type(e).__name__, error=str(e)[:200],
            )

        return response


def _slug(path: str) -> str:
    """Compress an HTTP path to an audit-friendly action key.

    /scopes                          → 'scopes'
    /documents/abc-123/archive       → 'documents.archive'
    /resurface/prepared-action/x/y   → 'resurface.prepared-action.x.y'   ← UUID stripped

    UUIDs in the path are dropped so audit aggregations cleanly group
    'all archives' rather than splaying per doc.
    """
    parts: list[str] = []
    for seg in path.strip("/").split("/"):
        if not seg:
            continue
        # Strip UUID-shaped segments
        if len(seg) == 36 and seg.count("-") == 4:
            continue
        parts.append(seg)
    return ".".join(parts) or "root"
