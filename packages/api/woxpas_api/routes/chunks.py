"""GET /chunks/{chunk_id} — lazy fetch for citation drill-downs.

Used by agents that received a synthesis answer with the LEAN citation
envelope (no chunk_text inline) and need the full chunk text to quote
verbatim or elaborate. Tenant-scoped — cross-tenant chunk_ids return 404
indistinguishably from "doesn't exist" (don't leak chunk-id existence
across tenants, Pillar 3).

Default returns the target chunk + 1 prev + 1 next chunk for surrounding
context. Caller can disable with `?context=0` if they only want the exact
chunk, or expand with `?context=2` for two-on-each-side (capped at 5).

Response shape:
  {
    "doc": {doc_id, title, filename, topics, processing_profile,
            sensitivity_class, created_at, last_accessed_at},
    "target": {chunk_id, ordinal, text, token_count},
    "prev":   [chunk_dict, ...],   // [] when context=0 or chunk is first
    "next":   [chunk_dict, ...]    // [] when context=0 or chunk is last
  }
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from woxpas_api.auth import tenant_id_dep
from woxpas_api.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(tags=["chunks"])


def _get_state(request: Request) -> AppState:
    return request.app.state.app


@router.get("/chunks/{chunk_id}")
async def get_chunk(
    chunk_id:  UUID,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    context:   Annotated[int, Query(ge=0, le=5)] = 1,
) -> dict:
    """Fetch a chunk by id with optional surrounding context.

    Returns 404 for both "chunk not in DB" and "chunk in another tenant" —
    the SQL filter is the security boundary; we don't differentiate so an
    attacker can't enumerate cross-tenant chunk_ids by status code.

    `context` defaults to 1 (one prev + one next chunk). Set to 0 for
    just the target. Capped at 5 to bound payload size."""
    state = _get_state(request)
    result = await state.retrieval.recall_chunk_with_context(
        chunk_id, tenant_id=tenant_id, context_window=context,
    )
    if result is None:
        # SAME response whether chunk doesn't exist OR is owned by another
        # tenant. Don't leak existence across tenants.
        log.info("chunks.get.not_found",
                 chunk_id=str(chunk_id), tenant_id=str(tenant_id))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"chunk {chunk_id} not found",
        )

    # Normalize the dict for JSON serialization (UUIDs, datetimes).
    return _to_jsonable(result)


def _to_jsonable(value):
    """Recursive UUID/datetime → str conversion. Mirrors the synthesize
    encoder; kept local here so the chunks route has no synthesis
    dependency."""
    from datetime import datetime
    from uuid import UUID as _UUID

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, _UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return str(value)
