"""Scope CRUD endpoints — Phase 5a.5.0 Group A.

Three endpoints:
  POST /scopes                                 — create (with embedding)
  POST /documents/{doc_id}/scopes              — assign by slug
  DELETE /documents/{doc_id}/scopes/{slug}     — unassign

Tenant-scoped via tenant_id_dep (RLS enforced through tenant_session in
the engine layer). Idempotency keys are deferred to 5a.5.4 hardening
(server-wide cache); for now repeat calls are naturally idempotent at
the data layer (UPSERT on create, ON CONFLICT DO NOTHING on assign,
no-op on unassign-when-missing).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from woxpas_storage import current_member_id

from woxpas_api.auth import require_min_role, tenant_id_dep
from woxpas_api.state import AppState, app_state

log = structlog.get_logger(__name__)

# Team memory (Phase A3): all scope routes here mutate (create/assign/unassign) —
# require member+. Dev / single_tenant_local (no member) pass through unchanged.
router = APIRouter(tags=["scopes"], dependencies=[Depends(require_min_role("member"))])


def _get_state(request: Request) -> AppState:
    return app_state(request)


# ─── Request/response models ───────────────────────────────────────────────

class CreateScopeRequest(BaseModel):
    """POST /scopes body. Slug is auto-derived from name when omitted."""
    model_config = ConfigDict(extra="forbid")

    name:            str = Field(min_length=1, max_length=200)
    description:     str | None = Field(default=None, max_length=2000)
    slug:            str | None = Field(default=None, max_length=120)
    parent_scope_id: UUID | None = None
    color:           str | None = Field(default=None, max_length=20)
    icon:            str | None = Field(default=None, max_length=80)
    embed:           bool = True
    """When False, the scope is created WITHOUT a Qdrant embedding,
    making it invisible to auto-assignment. Use only for catch-all
    scopes like 'Inbox' that should never auto-claim docs."""
    visibility:      str = Field(default="team", pattern="^(team|private)$")
    """Team memory (Phase A4): 'private' makes the scope visible only to the
    acting member (its owner) plus admins. Requires a resolved member
    (jwt mode with a member); a private scope cannot be created without one."""


class CreateScopeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scope_id:        UUID
    slug:            str
    embedded:        bool
    embedding_ref:   UUID | None = None
    already_existed: bool


class AssignScopeRequest(BaseModel):
    """POST /documents/{doc_id}/scopes body."""
    model_config = ConfigDict(extra="forbid")

    scope_slug:  str = Field(min_length=1, max_length=120)
    assigned_by: str = Field(default="agent", pattern="^(user|classifier|agent|adapter|rule)$")


class ScopeMutationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scope_id: UUID
    doc_id:   UUID
    inserted: bool
    """For assign: True if a NEW assignment was made (False = already
    assigned, idempotent). For unassign: True if a row was removed
    (False = was not assigned, no-op)."""


# ─── Routes ────────────────────────────────────────────────────────────────

@router.post("/scopes", response_model=CreateScopeResponse,
             status_code=status.HTTP_201_CREATED)
async def create_scope(
    body:      CreateScopeRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> CreateScopeResponse:
    """Create a new scope (or upsert if slug collides). Embeds
    name+description into Qdrant scopes_collection by default so
    summarize-stage auto-assignment can find it.

    Track 2 of the three-track scope model: agents call this when they
    need a new project home for an agent-saved capture and no existing
    scope matches. Pair with `assign_to_scope` to attach the doc."""
    state = _get_state(request)
    owner_member_id: UUID | None = None
    if body.visibility == "private":
        owner_member_id = current_member_id()
        if owner_member_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="a private scope needs an owning member, but none was "
                       "resolved. This is expected in dev / single_tenant_local "
                       "mode (no member context) — create a 'team' scope instead, "
                       "or use jwt mode where the token maps to a member.",
            )
    try:
        result = await state.scope_admin.create_scope(
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            slug=body.slug,
            parent_scope_id=body.parent_scope_id,
            color=body.color,
            icon=body.icon,
            embed=body.embed,
            visibility=body.visibility,
            owner_member_id=owner_member_id,
        )
        return CreateScopeResponse(
            scope_id=result.scope_id,
            slug=result.slug,
            embedded=result.embedded,
            embedding_ref=result.embedding_ref,
            already_existed=result.already_existed,
        )
    except Exception as e:
        log.error("scopes.create.failed",
                  tenant_id=str(tenant_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"create_scope failed: {type(e).__name__}",
        ) from e


@router.post("/documents/{doc_id}/scopes", response_model=ScopeMutationResponse)
async def assign_to_scope(
    doc_id:    UUID,
    body:      AssignScopeRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> ScopeMutationResponse:
    """Attach a doc to a scope by slug. Idempotent — re-assigning
    returns inserted=False without error. Multi-scope membership is
    allowed (a doc can belong to multiple scopes)."""
    state = _get_state(request)
    try:
        result = await state.scope_admin.assign_to_scope(
            tenant_id=tenant_id,
            doc_id=doc_id,
            scope_slug=body.scope_slug,
            assigned_by=body.assigned_by,
        )
        return ScopeMutationResponse(
            scope_id=result.scope_id, doc_id=result.doc_id,
            inserted=result.inserted,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        ) from e
    except Exception as e:
        log.error("scopes.assign.failed",
                  tenant_id=str(tenant_id), doc_id=str(doc_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"assign_to_scope failed: {type(e).__name__}",
        ) from e


@router.delete("/documents/{doc_id}/scopes/{scope_slug}",
               response_model=ScopeMutationResponse)
async def unassign_from_scope(
    doc_id:     UUID,
    scope_slug: str,
    request:    Request,
    tenant_id:  Annotated[UUID, Depends(tenant_id_dep)],
) -> ScopeMutationResponse:
    """Remove a doc-scope membership. Idempotent — calling on a non-
    existent assignment is a no-op (returns inserted=False).

    Three-track model: this is valid because unscoped docs are allowed
    (Track 1). The doc may end up unscoped after this call; the dreamer
    pass (Track 3a) will retry scope match on the next cycle."""
    state = _get_state(request)
    try:
        result = await state.scope_admin.unassign_from_scope(
            tenant_id=tenant_id,
            doc_id=doc_id,
            scope_slug=scope_slug,
        )
        return ScopeMutationResponse(
            scope_id=result.scope_id, doc_id=result.doc_id,
            inserted=result.inserted,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        ) from e
    except Exception as e:
        log.error("scopes.unassign.failed",
                  tenant_id=str(tenant_id), doc_id=str(doc_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"unassign_from_scope failed: {type(e).__name__}",
        ) from e
