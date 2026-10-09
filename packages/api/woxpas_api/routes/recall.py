"""POST /recall — single-entry retrieval endpoint.

Wraps RetrievalEngine.recall(). Tenant_id is plumbed from auth (header or
JWT depending on api_settings.auth_mode); never read from the request body.
This keeps the same request safely replayable across instances (cloud-
portability, pillar 4).

Versioned: response includes `protocol_version` so future cloud deploys
can negotiate compatibility. Request bodies that include unknown fields
are accepted (extra='ignore' on the type).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from woxpas_retrieval import RetrievalRequest, RetrievalResult

from woxpas_api.auth import tenant_id_dep
from woxpas_api.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(tags=["recall"])


def _get_state(request: Request) -> AppState:
    return request.app.state.app


@router.post("/recall", response_model=RetrievalResult)
async def recall(
    request:   Request,
    body:      RetrievalRequest,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> RetrievalResult:
    """Run a retrieval query through the orchestrator.

    Body shape: see RetrievalRequest in woxpas_retrieval.types.
    Tenant_id from auth, never from body.
    """
    state = _get_state(request)
    try:
        return await state.retrieval.recall(body, tenant_id=tenant_id)
    except Exception as e:
        log.error("recall.failed",
                  tenant_id=str(tenant_id),
                  query=body.query_text[:80],
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"recall failed: {type(e).__name__}",  # message kept in server logs only; not leaked to client
        ) from e
