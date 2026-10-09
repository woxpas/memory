"""Document endpoints — Phase 5a.5.0 Groups B + D.

State-change (Group B):
  POST /documents/{doc_id}/archive    — archive (excludes from retrieval)
  POST /documents/{doc_id}/unarchive  — restore

Full-document read (Group D):
  GET /documents/{doc_id}/full        — metadata + ALL chunks + optional
                                        canonical CDM + optional raw bytes

Hard delete is intentionally NOT exposed in v0.1.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from woxpas_api.auth import tenant_id_dep
from woxpas_api.state import AppState, app_state

log = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])


def _get_state(request: Request) -> AppState:
    return app_state(request)


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class DocumentStateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    doc_id:        UUID
    state_changed: bool
    archived_at:   str | None = None


@router.post("/documents/{doc_id}/archive", response_model=DocumentStateResponse)
async def archive_document(
    doc_id:    UUID,
    body:      ArchiveRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> DocumentStateResponse:
    """Archive a document. Idempotent (no-op if already archived).
    Archived docs are excluded from retrieval; their KG entities and
    relations remain (history-preserving). Unarchive to restore."""
    state = _get_state(request)
    try:
        result = await state.document_admin.archive_document(
            tenant_id=tenant_id, doc_id=doc_id, reason=body.reason,
        )
        return DocumentStateResponse(
            doc_id=result.doc_id,
            state_changed=result.state_changed,
            archived_at=result.archived_at,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        ) from e
    except Exception as e:
        log.error("documents.archive.failed",
                  tenant_id=str(tenant_id), doc_id=str(doc_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"archive failed: {type(e).__name__}",
        ) from e


@router.post("/documents/{doc_id}/unarchive", response_model=DocumentStateResponse)
async def unarchive_document(
    doc_id:    UUID,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> DocumentStateResponse:
    """Restore an archived document. Idempotent."""
    state = _get_state(request)
    try:
        result = await state.document_admin.unarchive_document(
            tenant_id=tenant_id, doc_id=doc_id,
        )
        return DocumentStateResponse(
            doc_id=result.doc_id,
            state_changed=result.state_changed,
            archived_at=result.archived_at,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        ) from e
    except Exception as e:
        log.error("documents.unarchive.failed",
                  tenant_id=str(tenant_id), doc_id=str(doc_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"unarchive failed: {type(e).__name__}",
        ) from e


# ─── Group D: full document retrieval ───────────────────────────────────


class FullDocChunkResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chunk_id:    UUID
    ordinal:     int
    text:        str
    token_count: int


class DocumentFullResponse(BaseModel):
    """GET /documents/{doc_id}/full response. Mirrors the CLI inspect-doc
    capability via HTTP/MCP.

    Sensitivity: callers requesting include_canonical/include_raw_bytes
    on a sensitive doc are responsible for cloud-policy compliance —
    memory does not gate the response by the cloud-egress flag (this is
    a tenant-internal read, not a cloud send). Synthesis layer's gate
    fires when those payloads later flow into a cloud LLM call."""
    model_config = ConfigDict(extra="ignore")

    doc_id:             UUID
    title:              str | None = None
    filename:           str | None = None
    summary:            str | None = None
    topics:             list[str] = Field(default_factory=list)
    processing_profile: str | None = None
    sensitivity_class:  str = "standard"
    captured_by:        str | None = None
    source_channel:     str | None = None
    status:             str | None = None
    archived_at:        str | None = None
    scope_slugs:        list[str] = Field(default_factory=list)
    created_at:         str | None = None

    chunks:             list[FullDocChunkResponse] = Field(default_factory=list)
    canonical_payload:  dict[str, Any] | None = None
    raw_bytes_b64:      str | None = None
    raw_size_bytes:     int | None = None
    metrics:            dict[str, Any] = Field(default_factory=dict)


@router.get("/documents/{doc_id}/full", response_model=DocumentFullResponse)
async def get_document_full(
    doc_id:    UUID,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    include_chunks:    Annotated[bool, Query()] = True,
    include_canonical: Annotated[bool, Query()] = False,
    include_raw_bytes: Annotated[bool, Query()] = False,
) -> DocumentFullResponse:
    """Fetch the full document — metadata + all chunks + optional
    decrypted canonical CDM + optional decrypted raw bytes.

    Direct fetch by id; works on archived docs (unlike /recall which
    excludes them). For semantic search, use /recall. For preview-only
    fetch, use /chunks/{id}.

    `include_canonical=true` returns the structured CDM payload (text +
    structure) — useful when the agent needs more than chunk-level text.
    `include_raw_bytes=true` returns the original bytes b64-encoded —
    rarely useful; opt-in only."""
    state = _get_state(request)
    try:
        result = await state.document_read.get_document_full(
            tenant_id=tenant_id, doc_id=doc_id,
            include_chunks=include_chunks,
            include_canonical=include_canonical,
            include_raw_bytes=include_raw_bytes,
        )
        return DocumentFullResponse(
            doc_id=result.doc_id,
            title=result.title,
            filename=result.filename,
            summary=result.summary,
            topics=result.topics,
            processing_profile=result.processing_profile,
            sensitivity_class=result.sensitivity_class,
            captured_by=result.captured_by,
            source_channel=result.source_channel,
            status=result.status,
            archived_at=result.archived_at,
            scope_slugs=result.scope_slugs,
            created_at=result.created_at,
            chunks=[
                FullDocChunkResponse(
                    chunk_id=c.chunk_id, ordinal=c.ordinal,
                    text=c.text, token_count=c.token_count,
                )
                for c in result.chunks
            ],
            canonical_payload=result.canonical_payload,
            raw_bytes_b64=result.raw_bytes_b64,
            raw_size_bytes=result.raw_size_bytes,
            metrics=result.metrics,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        ) from e
    except Exception as e:
        log.error("documents.get_full.failed",
                  tenant_id=str(tenant_id), doc_id=str(doc_id),
                  error_class=type(e).__name__, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"get_document_full failed: {type(e).__name__}",
        ) from e
