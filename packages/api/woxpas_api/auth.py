"""Tenant identity resolution for inbound requests.

Three modes (selected via API_AUTH_MODE):

  dev                  — reads X-Woxpas-Tenant-Id; missing header → 401.
                         Logs a warning each request. Use on trusted local
                         machine.
  single_tenant_local  — same as dev when header is present; falls back to
                         WOXPAS_DEV_TENANT_ID_DEFAULT when missing.
                         Pillar-3 hardening: refuses fallback when the API
                         is bound to a non-loopback interface, so this mode
                         can't be accidentally exposed to a network.
  jwt                  — verifies a Bearer token (RS256/ES256), reads
                         tenant_id from 'woxpas_tenant_id' or 'sub' claim.

Single dependency: `tenant_id_dep`. The dependency reads the auth mode
from request.app.state at call time, so settings can flip without redeploys.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Annotated, Any
from uuid import UUID

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from woxpas_storage import (
    MemberRepo,
    current_member_role,
    role_allows,
    set_current_member_id,
    set_current_member_role,
)

from woxpas_api.settings import JwtSettings

# Loopback addresses that single_tenant_local mode is safe on.
# An admin who sets API_HOST=0.0.0.0 should NOT get implicit tenant
# resolution — that would expose the install to the network.
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1", "localhost", "::1",
})

log = structlog.get_logger(__name__)

_tenant_var: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)


def current_tenant_id() -> UUID:
    """Read the tenant id resolved earlier in this request.

    Useful inside services that don't take the dependency directly. Routes
    should still depend on `tenant_id_dep` so the verifier runs.
    """
    tid = _tenant_var.get()
    if tid is None:
        raise HTTPException(401, "no tenant context")
    return tid


# ─── single public dependency ──────────────────────────────────────────────

async def tenant_id_dep(request: Request) -> UUID:
    """FastAPI dependency. Resolves tenant from headers/JWT based on auth_mode."""
    mode = request.app.state.api_settings.auth_mode
    if mode == "dev":
        return await _dev_resolve(request, allow_default=False)
    if mode == "single_tenant_local":
        return await _dev_resolve(request, allow_default=True)
    if mode == "jwt":
        return await _jwt_resolve(request)
    raise HTTPException(500, f"unknown auth_mode: {mode}")


# ─── verifiers ─────────────────────────────────────────────────────────────

async def _dev_resolve(request: Request, *, allow_default: bool) -> UUID:
    # Phase 5a.5+ Pillar-3 hardening: dev mode is permissive (any header-
    # supplied tenant_id is accepted as that tenant). Block dev mode on
    # non-loopback hosts so a one-line misconfiguration (API_HOST=0.0.0.0
    # without flipping auth_mode to 'jwt') doesn't expose the install
    # to the network as a multi-tenant free-for-all.
    api_host = request.app.state.api_settings.host
    if api_host not in _LOOPBACK_HOSTS:
        log.warning(
            "auth.dev_mode_blocked_non_loopback",
            api_host=api_host, path=request.url.path,
        )
        raise HTTPException(
            401,
            f"dev/single_tenant_local mode requires API_HOST in {sorted(_LOOPBACK_HOSTS)}; "
            f"got {api_host!r}. Set API_AUTH_MODE=jwt for non-loopback deployments.",
        )

    raw = request.headers.get("X-Woxpas-Tenant-Id")
    if not raw and allow_default:
        # We've already enforced loopback above; safe to fall back to
        # the env-supplied default.
        default = os.environ.get("WOXPAS_DEV_TENANT_ID_DEFAULT", "").strip()
        if not default:
            raise HTTPException(
                401,
                "single_tenant_local mode requires WOXPAS_DEV_TENANT_ID_DEFAULT "
                "to be set in env; or send X-Woxpas-Tenant-Id explicitly",
            )
        try:
            tid = UUID(default)
        except ValueError as e:
            raise HTTPException(
                500,
                f"WOXPAS_DEV_TENANT_ID_DEFAULT is not a valid UUID: {e}",
            ) from e
        _tenant_var.set(tid)
        request.state.tenant_id_for_audit = tid
        # debug-level: don't spam logs in normal local use
        log.debug("auth.single_tenant_local_default",
                  path=request.url.path, tenant_id=str(tid))
        return tid

    if not raw:
        raise HTTPException(401, "missing X-Woxpas-Tenant-Id (dev mode)")
    try:
        tid = UUID(raw)
    except ValueError as e:
        raise HTTPException(400, f"invalid tenant id: {e}") from e
    _tenant_var.set(tid)
    request.state.tenant_id_for_audit = tid
    log.warning("auth.dev_mode_request",
                path=request.url.path, tenant_id=str(tid))
    return tid


async def _jwt_resolve(request: Request) -> UUID:
    settings = JwtSettings()
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    pk = settings.public_key.get_secret_value()
    if not pk:
        raise HTTPException(500, "jwt public key not configured")
    try:
        claims = jwt.decode(
            token, pk,
            algorithms=settings.algorithms,
            audience=settings.audience,
            issuer=settings.issuer,
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e
    raw = claims.get("woxpas_tenant_id") or claims.get("sub")
    if not raw:
        raise HTTPException(401, "no tenant claim")
    try:
        tid = UUID(raw)
    except ValueError as e:
        raise HTTPException(401, f"invalid tenant claim: {e}") from e
    _tenant_var.set(tid)
    request.state.tenant_id_for_audit = tid

    # Team memory (Phase A2): best-effort member attribution. If the 'sub' claim
    # maps to an active member of this team, record them for attribution; else
    # leave unattributed (NULL). Non-breaking — membership is NOT required here
    # (hard role enforcement is a later sub-phase), and a lookup failure never
    # breaks auth (Pillar 3: liveness). `sub` is only a distinct user when the
    # tenant came from the woxpas_tenant_id claim; a legacy sub==tenant token
    # simply resolves no member.
    subject = claims.get("sub")
    resolved = False
    if subject and subject != raw:
        resolved = await _maybe_attribute_member(request, tid, subject)

    # Team memory (Phase A3): hard membership gate. When enabled, a token whose
    # subject is not an active member of this team is rejected. Off by default
    # (A2 stays non-breaking); teams opt in via API_REQUIRE_MEMBERSHIP=true.
    if request.app.state.api_settings.require_membership and not resolved:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "not an active member of this team",
        )
    return tid


async def _maybe_attribute_member(request: Request, tenant_id: UUID, subject: str) -> bool:
    """Resolve a JWT subject to an active team member and record it (id + role)
    for attribution and role gating (team memory). Best-effort: swallows any
    failure. Returns True iff an active member was resolved."""
    app_state = getattr(request.app.state, "app", None)
    if app_state is None:
        return False
    try:
        pool = app_state.ctx.pool
        async with pool.tenant_session(tenant_id) as conn:
            member = await MemberRepo(conn).get_by_subject(tenant_id, subject)
    except Exception as e:  # attribution must never break auth
        log.warning("auth.member_resolve_failed",
                    error_class=type(e).__name__, error=str(e)[:200])
        return False
    if member and member.get("status") == "active":
        set_current_member_id(member["member_id"])
        set_current_member_role(member.get("role"))
        request.state.member_id_for_audit = member["member_id"]
        return True
    return False


# ─── role gate (team memory, Phase A3) ───────────────────────────────────────

def require_min_role(minimum: str) -> Any:
    """FastAPI dependency factory: 403 unless the acting member's role meets
    `minimum` (viewer < member < admin < owner).

    Fail-closed in jwt mode: a request that did NOT resolve to a member (role
    None) is DENIED on a gated route — a validly-signed token whose subject
    isn't a member must not inherit full access just because membership
    enforcement (API_REQUIRE_MEMBERSHIP) is off. The None=full-access exemption
    applies ONLY to dev / single_tenant_local (loopback, single-user, no member
    concept).

    Depend on this AFTER tenant_id_dep so the member/role is resolved first.
    """
    async def _dep(
        request: Request, _tid: Annotated[UUID, Depends(tenant_id_dep)],
    ) -> None:
        role = current_member_role()
        if role is None:
            mode = request.app.state.api_settings.auth_mode
            if mode == "jwt":
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"this action requires a team member (role {minimum}+); "
                    "the token did not resolve to an active member",
                )
            return  # dev / single_tenant_local: single-user, no member gate
        if not role_allows(role, minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {role!r} is not permitted here (requires {minimum}+)",
            )
    return _dep
