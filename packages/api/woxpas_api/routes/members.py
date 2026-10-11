"""Team member administration (team memory, Phase A3).

  GET    /members            — list the roster            (member+)
  PATCH  /members/{id}       — change role / status        (admin+)
  DELETE /members/{id}       — revoke access (status=removed) (admin+)

Role-gated via require_min_role. Inviting a NEW member (which must re-wrap the
shared DEK) is intentionally NOT here: in jwt mode the server has no admin
passphrase to unwrap with, so member creation stays with `woxpas-cli add-member`
until the invite-flow design is settled (see docs/design/team-memory.md §10).

Tenant-scoped via tenant_id_dep; RLS enforced through tenant_session.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from woxpas_storage import MemberRepo

from woxpas_api.auth import require_min_role, tenant_id_dep
from woxpas_api.state import AppState, app_state

log = structlog.get_logger(__name__)

router = APIRouter(tags=["members"])

_VALID_ROLES = {"owner", "admin", "member", "viewer"}
_VALID_STATUS = {"active", "suspended", "removed"}


def _get_state(request: Request) -> AppState:
    return app_state(request)


class MemberPatch(BaseModel):
    """PATCH /members/{id} body. At least one field must be set."""
    model_config = ConfigDict(extra="forbid")
    role:   str | None = Field(default=None)
    status: str | None = Field(default=None)


@router.get("/members", dependencies=[Depends(require_min_role("member"))])
async def list_members(
    request: Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    include_removed: bool = False,
) -> dict[str, object]:
    state = _get_state(request)
    async with state.ctx.pool.tenant_session(tenant_id) as conn:
        members = await MemberRepo(conn).list_members(
            tenant_id, include_removed=include_removed,
        )
    return {"members": members, "count": len(members)}


@router.patch("/members/{member_id}", dependencies=[Depends(require_min_role("admin"))])
async def patch_member(
    request: Request,
    member_id: UUID,
    body: MemberPatch,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> dict[str, object]:
    if body.role is None and body.status is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to update")
    if body.role is not None and body.role not in _VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid role: {body.role!r}")
    if body.status is not None and body.status not in _VALID_STATUS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid status: {body.status!r}")

    async with state_pool(request).tenant_session(tenant_id) as conn:
        repo = MemberRepo(conn)
        target = await repo.get(tenant_id, member_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such member")

        # Guard: never strand a team without an active owner.
        demotes_owner = (
            target["role"] == "owner"
            and (
                (body.role is not None and body.role != "owner")
                or (body.status is not None and body.status != "active")
            )
        )
        if demotes_owner and await repo.count_active_owners(tenant_id) <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "cannot demote/deactivate the last active owner — promote another "
                "member to owner first",
            )
        if body.role is not None:
            await repo.set_role(tenant_id, member_id, body.role)
        if body.status is not None:
            await repo.set_status(tenant_id, member_id, body.status)
        updated = await repo.get(tenant_id, member_id)
    return {"member": updated}


@router.delete("/members/{member_id}", dependencies=[Depends(require_min_role("admin"))])
async def remove_member(
    request: Request,
    member_id: UUID,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> dict[str, object]:
    async with state_pool(request).tenant_session(tenant_id) as conn:
        repo = MemberRepo(conn)
        target = await repo.get(tenant_id, member_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such member")
        if target["role"] == "owner" and await repo.count_active_owners(tenant_id) <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "cannot remove the last active owner — promote another member first",
            )
        await repo.set_status(tenant_id, member_id, "removed")
    return {"removed": str(member_id)}


def state_pool(request: Request):  # type: ignore[no-untyped-def]  # returns PostgresPool; AppState is import-cycle sensitive
    return _get_state(request).ctx.pool
