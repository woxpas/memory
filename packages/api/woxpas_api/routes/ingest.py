"""POST /ingest — file upload + text capture.

Two entry points:
  POST /ingest/file  — multipart upload (channel: file_upload)
  POST /ingest/text  — JSON text body (channel: api_custom or mcp_inbound)

Both feed the IDENTICAL downstream pipeline (ingress → AV → detect → CDM
→ summarize → chunk → embed → indexed). The only difference is the HTTP
shape and the source_channel marker.

Other channels (voice, browser_extension, slack, agent_log, calendar)
get their own routes when adapters land in v0.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from woxpas_adapters.base import IngestPayload
from woxpas_core import CapturedBy, ContentRef, IngestEnvelope, SourceChannel
from woxpas_pipeline.ingress import DuplicateDocument
from woxpas_storage import SourceRepo

from woxpas_api.auth import tenant_id_dep
from woxpas_api.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(tags=["ingest"])


def _get_state(request: Request) -> AppState:
    return request.app.state.app


@router.post("/ingest/file", status_code=status.HTTP_202_ACCEPTED)
async def ingest_file(
    request: Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    file: Annotated[UploadFile, File()],
    suggested_title: Annotated[str | None, Form()] = None,
    suggested_processing_profile: Annotated[str | None, Form()] = None,
    suggested_scopes: Annotated[str | None, Form()] = None,        # comma-sep slugs
    thread_hint: Annotated[str | None, Form()] = None,
    valid_time_hint: Annotated[str | None, Form()] = None,         # ISO 8601
    captured_by: Annotated[str | None, Form()] = None,             # user|agent|system
    sensitivity: Annotated[str | None, Form()] = None,             # standard|sensitive (Tier 1 manual flag)
    on_duplicate: Annotated[str, Form()] = "return_existing",
) -> JSONResponse:
    if sensitivity is not None and sensitivity not in {"standard", "sensitive"}:
        raise HTTPException(
            400, "sensitivity must be 'standard' or 'sensitive' (or omitted)",
        )
    state = _get_state(request)

    # 1. Read bytes (size guarded by API settings).
    api_settings = request.app.state.api_settings
    content = await file.read()
    if not content:
        raise HTTPException(400, "empty payload")
    if len(content) > api_settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file exceeds {api_settings.max_upload_bytes} bytes",
        )

    # 2. Resolve / register the source row for this channel.
    async with state.ctx.pool.tenant_session(tenant_id) as conn:
        source_id = await SourceRepo(conn).get_or_create(
            tenant_id, "file_upload", "default-file-upload",
            description="HTTP file uploads via API gateway",
        )

    # 3. Build the envelope via the file adapter.
    payload = await state.file_adapter.build_envelope(
        tenant_id=tenant_id,
        source_id=source_id,
        content=content,
        filename=file.filename,
        mime_hint=file.content_type,
        suggested_title=suggested_title,
        suggested_processing_profile=suggested_processing_profile,
        suggested_scopes=[s.strip() for s in (suggested_scopes or "").split(",") if s.strip()],
        thread_hint=UUID(thread_hint) if thread_hint else None,
        valid_time_hint=datetime.fromisoformat(valid_time_hint) if valid_time_hint else None,
        captured_by=CapturedBy(captured_by) if captured_by else CapturedBy.USER,
        sensitivity_class=sensitivity,
    )

    # 4. Hand to ingress.
    try:
        doc_id, was_new = await state.ingress.submit(
            payload, on_duplicate=on_duplicate,
        )
    except DuplicateDocument as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "duplicate", "doc_id": str(e.doc_id)},
        )
    except Exception as e:
        log.error("ingest.failed", error=str(e), tenant_id=str(tenant_id))
        raise HTTPException(500, f"ingest failed: {e}") from e

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "doc_id": str(doc_id),
            "status": "received",
            "deduped": not was_new,
        },
    )


# ─── /ingest/text — Phase 5a.5.0 Group C ────────────────────────────────


class IngestTextRequest(BaseModel):
    """JSON body for POST /ingest/text. Same downstream pipeline as
    /ingest/file; the only difference is content arrives as a string
    in the JSON body instead of as a multipart upload.

    Best practice for AGENT callers (Track 2 of three-track scope model):
    provide `suggested_scope_slugs` derived from conversation context.
    If no matching scope exists, call `/scopes` to create one first."""
    model_config = ConfigDict(extra="forbid")

    content_text: str = Field(min_length=1, max_length=5_000_000)
    """The raw text to ingest. UTF-8. Max 5MB."""

    suggested_title:              str | None = Field(default=None, max_length=500)
    suggested_processing_profile: str | None = Field(default=None, max_length=80)
    suggested_scope_slugs:        list[str]  = Field(default_factory=list)
    thread_hint:                  UUID | None = None
    valid_time_hint:              datetime | None = None
    sensitivity_class:            str | None = Field(default=None, pattern="^(standard|sensitive)$")

    captured_by:    str = Field(default="agent", pattern="^(user|agent|system)$")
    """Default 'agent' — text-body ingest is typically driven by an agent
    storing a conversational memory. Set to 'user' for direct user-driven
    captures (CLI, notes app webhook, etc)."""

    source_channel: str = Field(default="api_custom",
                                pattern="^(api_custom|mcp_inbound|agent|browser_extension)$")
    """Where the text came from. Defaults to api_custom for direct HTTP
    callers; MCP server should set mcp_inbound."""

    on_duplicate: str = Field(default="return_existing",
                              pattern="^(return_existing|fail|create_new)$")


@router.post("/ingest/text", status_code=status.HTTP_202_ACCEPTED)
async def ingest_text(
    body:      IngestTextRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> JSONResponse:
    """Ingest raw text content. Same pipeline as /ingest/file; just a
    different entry shape.

    Returns 202 with doc_id immediately; pipeline runs async on the
    worker (mirrors /ingest/file behavior). Poll /documents/{id}/full
    or filter `documents.status='indexed'` to know when ready."""
    state = _get_state(request)
    api_settings = request.app.state.api_settings

    content_bytes = body.content_text.encode("utf-8")
    if len(content_bytes) > api_settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"text exceeds {api_settings.max_upload_bytes} bytes",
        )

    # Resolve / register source row keyed on the chosen channel.
    async with state.ctx.pool.tenant_session(tenant_id) as conn:
        source_id = await SourceRepo(conn).get_or_create(
            tenant_id, body.source_channel,
            f"default-{body.source_channel}",
            description=f"Text-body ingest via {body.source_channel}",
        )

    # Build envelope inline. Mirror FileAdapter's pattern: content_ref
    # uses inline:; ingress rewrites to s3:// after persisting.
    envelope = IngestEnvelope(
        tenant_id=tenant_id,
        source_id=source_id,
        source_channel=SourceChannel(body.source_channel),
        captured_by=CapturedBy(body.captured_by),
        capture_at=datetime.now(UTC),
        content_ref=ContentRef(uri="inline:"),
        mime="text/plain",
        filename=None,
        suggested_title=body.suggested_title,
        suggested_processing_profile=body.suggested_processing_profile,
        suggested_scopes=list(body.suggested_scope_slugs or []),
        thread_hint=body.thread_hint,
        valid_time_hint=body.valid_time_hint,
        sensitivity_class=body.sensitivity_class,
    )
    payload = IngestPayload(envelope=envelope, content=content_bytes)

    try:
        doc_id, was_new = await state.ingress.submit(
            payload, on_duplicate=body.on_duplicate,
        )
    except DuplicateDocument as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "duplicate", "doc_id": str(e.doc_id)},
        )
    except Exception as e:
        log.error("ingest.text.failed",
                  tenant_id=str(tenant_id), error=str(e))
        raise HTTPException(500, f"ingest text failed: {e}") from e

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "doc_id": str(doc_id),
            "status": "received",
            "deduped": not was_new,
        },
    )
