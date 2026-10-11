"""Conflict endpoints — Phase 5a.5.0 Group E.

GET  /conflicts                — list (filters: state, severity, kind)
GET  /conflicts/{id}           — full detail with suggested shapes hint
POST /conflicts/{id}/resolve   — apply a resolution shape

Resolution shapes (see project_conflict_resolution_shapes.md):
  pick         — winner + loser
  coexistence  — discriminators (context-based both-correct)
  progression  — valid_times (temporal evolution)
  dismissed    — false-positive / agree-to-disagree

State transitions (open is the only writable starting state in v0.1):
  open → human_resolved   (pick or progression)
  open → living           (coexistence)
  open → dismissed
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from woxpas_api.auth import require_min_role, tenant_id_dep
from woxpas_api.state import AppState, app_state

log = structlog.get_logger(__name__)

router = APIRouter(tags=["conflicts"])


def _get_state(request: Request) -> AppState:
    return app_state(request)


# ─── Models ────────────────────────────────────────────────────────────────


class ConflictListItemResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conflict_id:        UUID
    kind:               str
    severity:           str
    predicate:          str | None = None
    notes:              str | None = None
    detected_at:        str
    subject_entity_ids: list[UUID] = Field(default_factory=list)
    doc_refs:           list[UUID] = Field(default_factory=list)


class ConflictListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[ConflictListItemResponse] = Field(default_factory=list)
    total: int = 0


class ConflictDetailResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conflict_id:        UUID
    kind:               str
    severity:           str
    predicate:          str | None = None
    notes:              str | None = None
    detected_at:        str
    resolved_state:     str
    resolved_value:     dict[str, Any] | None = None
    resolved_at:        str | None = None
    subject_entity_ids: list[UUID] = Field(default_factory=list)
    doc_refs:           list[UUID] = Field(default_factory=list)
    mention_refs:       list[dict[str, Any]] = Field(default_factory=list)
    suggested_shapes:   list[str] = Field(default_factory=list)


class ResolveConflictRequest(BaseModel):
    """Body for POST /conflicts/{id}/resolve. Discriminated by `kind`.

    Required-field semantics per shape:
      pick:        winner + loser (entity_ids or claim_ids)
      coexistence: discriminators (e.g. {context:'home'} or {time_window:'2020-2022'})
      progression: valid_times (e.g. {old:'2020..2022', new:'2022..'})
      dismissed:   no extra fields required; note recommended
    """
    model_config = ConfigDict(extra="forbid")

    kind:           Literal["pick", "coexistence", "progression", "dismissed"]
    winner:         UUID | None = None
    loser:          UUID | None = None
    discriminators: dict[str, str] | None = None
    valid_times:    dict[str, str] | None = None
    note:           str | None = Field(default=None, max_length=2000)
    resolved_by:    str = Field(default="agent", pattern="^(user|agent|automatic)$")


class ConflictResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conflict_id: UUID
    kind:        str
    new_state:   str


# ─── Routes ────────────────────────────────────────────────────────────────


@router.get("/conflicts", response_model=ConflictListResponse)
async def list_conflicts(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    state:     Annotated[str, Query(pattern="^(open|auto_resolved|human_resolved|living|dismissed|all)$")] = "open",
    severity:  Annotated[str | None, Query(pattern="^(critical|high|medium|low)$")] = None,
    kind:      Annotated[str | None, Query(max_length=80)] = None,
    limit:     Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConflictListResponse:
    """List conflicts for this tenant. Default: open conflicts ordered by
    severity (critical first), then detected_at desc."""
    state_engine = _get_state(request)
    try:
        items = await state_engine.conflict.list_conflicts(
            tenant_id=tenant_id, state=state, severity=severity,
            kind=kind, limit=limit,
        )
        return ConflictListResponse(
            items=[
                ConflictListItemResponse(
                    conflict_id=i.conflict_id, kind=i.kind,
                    severity=i.severity, predicate=i.predicate,
                    notes=i.notes, detected_at=i.detected_at,
                    subject_entity_ids=i.subject_entity_ids,
                    doc_refs=i.doc_refs,
                )
                for i in items
            ],
            total=len(items),
        )
    except Exception as e:
        log.error("conflicts.list.failed",
                  tenant_id=str(tenant_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"list_conflicts failed: {type(e).__name__}",
        ) from e


@router.get("/conflicts/{conflict_id}", response_model=ConflictDetailResponse)
async def get_conflict(
    conflict_id: UUID,
    request:     Request,
    tenant_id:   Annotated[UUID, Depends(tenant_id_dep)],
) -> ConflictDetailResponse:
    """Full conflict detail including mention_refs (per-side claims with
    chunk provenance) + suggested resolution shapes."""
    state_engine = _get_state(request)
    try:
        d = await state_engine.conflict.get_conflict(
            tenant_id=tenant_id, conflict_id=conflict_id,
        )
        return ConflictDetailResponse(
            conflict_id=d.conflict_id, kind=d.kind, severity=d.severity,
            predicate=d.predicate, notes=d.notes, detected_at=d.detected_at,
            resolved_state=d.resolved_state, resolved_value=d.resolved_value,
            resolved_at=d.resolved_at,
            subject_entity_ids=d.subject_entity_ids,
            doc_refs=d.doc_refs, mention_refs=d.mention_refs,
            suggested_shapes=d.suggested_shapes,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        ) from e
    except Exception as e:
        log.error("conflicts.get.failed",
                  tenant_id=str(tenant_id), conflict_id=str(conflict_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"get_conflict failed: {type(e).__name__}",
        ) from e


@router.post("/conflicts/{conflict_id}/resolve",
             response_model=ConflictResolutionResponse,
             dependencies=[Depends(require_min_role("member"))])
async def resolve_conflict(
    conflict_id: UUID,
    body:        ResolveConflictRequest,
    request:     Request,
    tenant_id:   Annotated[UUID, Depends(tenant_id_dep)],
) -> ConflictResolutionResponse:
    """Apply a resolution to an open conflict. Returns the new
    resolved_state. Re-resolving an already-resolved conflict is NOT
    supported in v0.1 (returns 409). Already-dismissed conflicts are
    final."""
    state_engine = _get_state(request)
    try:
        result = await state_engine.conflict.resolve_conflict(
            tenant_id=tenant_id, conflict_id=conflict_id,
            kind=body.kind,
            winner=body.winner, loser=body.loser,
            discriminators=body.discriminators,
            valid_times=body.valid_times,
            note=body.note,
            resolved_by=body.resolved_by,
        )
        return ConflictResolutionResponse(
            conflict_id=result.conflict_id, kind=result.kind,
            new_state=result.new_state,
        )
    except ValueError as e:
        # Distinguish "not found" from "already resolved" / validation
        msg = str(e)
        if "not found" in msg:
            code = status.HTTP_404_NOT_FOUND
        elif "already resolved" in msg:
            code = status.HTTP_409_CONFLICT
        else:
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=msg) from e
    except Exception as e:
        log.error("conflicts.resolve.failed",
                  tenant_id=str(tenant_id), conflict_id=str(conflict_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resolve_conflict failed: {type(e).__name__}",
        ) from e
