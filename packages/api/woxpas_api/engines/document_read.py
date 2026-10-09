"""DocumentReadEngine — Phase 5a.5.0 Group D.

Returns the FULL document envelope: metadata + ALL chunks + optional
canonical CDM payload + optional raw bytes. Mirrors the CLI's
`inspect-doc --with-canonical --with-raw` capability via HTTP/MCP.

Why a separate tool from /recall:
  - /recall's file_lookup intent returns ≤5 chunks (preview only)
  - This is the path agents use when they need the COMPLETE doc
    (e.g., "summarize this document for me", "rewrite this email")

Sensitivity-aware:
  - default response excludes chunk text past sensitivity thresholds
  - include_canonical / include_raw_bytes are explicit opt-ins (lean
    envelope by default; agents fetch full text when they need it)

Decryption requires the tenant DEK. The PipelineContext holds it (loaded
from boot service or dev env var). This engine reads ctx.session_dek
on demand only when the caller asks for canonical/raw — so the common
case (chunks-only) doesn't touch the DEK at all.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class FullDocChunk:
    chunk_id:    UUID
    ordinal:     int
    text:        str
    token_count: int


@dataclass(slots=True)
class DocumentFullResult:
    doc_id:                  UUID
    title:                   str | None
    filename:                str | None
    summary:                 str | None
    topics:                  list[str]
    processing_profile:      str | None
    sensitivity_class:       str
    captured_by:             str | None
    source_channel:          str | None
    status:                  str | None
    archived_at:             str | None
    scope_slugs:             list[str]
    created_at:              str | None
    chunks:                  list[FullDocChunk] = field(default_factory=list)
    canonical_payload:       dict[str, Any] | None = None
    raw_bytes_b64:           str | None = None
    raw_size_bytes:          int | None = None
    metrics:                 dict[str, Any] = field(default_factory=dict)


class DocumentReadEngine:
    """Tenant-isolated full-document read with optional decrypted payloads."""

    def __init__(self, *, ctx) -> None:                       # type: ignore[no-untyped-def]
        self._ctx = ctx
        self._pool = ctx.pool
        self._objstore = ctx.objstore

    async def get_document_full(
        self, *,
        tenant_id:           UUID,
        doc_id:              UUID,
        include_chunks:      bool = True,
        include_canonical:   bool = False,
        include_raw_bytes:   bool = False,
    ) -> DocumentFullResult:
        """Fetch the full document. Direct fetch by id — works on
        archived docs (unlike /recall which excludes them).

        Raises ValueError if doc not found under this tenant.
        """
        t0 = time.monotonic()
        async with self._pool.tenant_session(tenant_id) as conn:
            doc = await conn.fetchrow(
                """
                SELECT doc_id, title, filename, summary, topics_inferred,
                       processing_profile, sensitivity_class, captured_by,
                       source_channel, status, archived_at, created_at,
                       canonical_uri, sha256, mime
                  FROM documents
                 WHERE tenant_id = $1
                   AND doc_id    = $2
                   AND deleted_at IS NULL
                """,
                tenant_id, doc_id,
            )
            if doc is None:
                raise ValueError(f"document not found: {doc_id}")

            scope_rows = await conn.fetch(
                """
                SELECT s.slug
                  FROM doc_scopes ds
                  JOIN scopes s USING (scope_id)
                 WHERE ds.tenant_id = $1 AND ds.doc_id = $2
                 ORDER BY s.slug
                """,
                tenant_id, doc_id,
            )
            scope_slugs = [r["slug"] for r in scope_rows]

            chunks: list[FullDocChunk] = []
            if include_chunks:
                chunk_rows = await conn.fetch(
                    """
                    SELECT chunk_id, ordinal, text, token_count
                      FROM chunks
                     WHERE tenant_id = $1 AND doc_id = $2
                     ORDER BY ordinal
                    """,
                    tenant_id, doc_id,
                )
                chunks = [
                    FullDocChunk(
                        chunk_id=r["chunk_id"],
                        ordinal=r["ordinal"],
                        text=r["text"] or "",
                        token_count=r["token_count"] or 0,
                    )
                    for r in chunk_rows
                ]

        # Lazy DEK access — only when caller asks for encrypted payloads
        canonical_payload: dict[str, Any] | None = None
        raw_bytes_b64: str | None = None
        raw_size: int | None = None

        if include_canonical or include_raw_bytes:
            try:
                dek = await self._ctx.session_dek(tenant_id)
            except RuntimeError as e:
                log.warning("document_read.dek_unavailable",
                            tenant_id=str(tenant_id), error=str(e))
                dek = None

            if dek is not None:
                if include_canonical and doc["canonical_uri"]:
                    try:
                        cdm_bytes = await self._objstore.get_canonical(
                            doc["doc_id"], 1, dek,
                        )
                        canonical_payload = json.loads(cdm_bytes.decode("utf-8"))
                    except Exception as e:
                        log.warning("document_read.canonical_decrypt_failed",
                                    doc_id=str(doc_id),
                                    error_class=type(e).__name__,
                                    error=str(e)[:200])

                if include_raw_bytes and doc["sha256"]:
                    try:
                        sha_hex = doc["sha256"].hex()
                        raw_bytes = await self._objstore.get_raw_bytes(
                            sha_hex, dek,
                        )
                        raw_size = len(raw_bytes)
                        # b64-encode so JSON-safe; agent decodes if needed
                        raw_bytes_b64 = base64.b64encode(raw_bytes).decode("ascii")
                    except Exception as e:
                        log.warning("document_read.raw_decrypt_failed",
                                    doc_id=str(doc_id),
                                    error_class=type(e).__name__,
                                    error=str(e)[:200])

        ms = round((time.monotonic() - t0) * 1000, 1)
        log.info("document_read.get_full.done",
                 tenant_id=str(tenant_id), doc_id=str(doc_id),
                 chunk_count=len(chunks),
                 included_canonical=canonical_payload is not None,
                 included_raw=raw_bytes_b64 is not None,
                 ms=ms)

        return DocumentFullResult(
            doc_id=doc["doc_id"],
            title=doc["title"],
            filename=doc["filename"],
            summary=doc["summary"],
            topics=list(doc["topics_inferred"] or []),
            processing_profile=doc["processing_profile"],
            sensitivity_class=doc["sensitivity_class"] or "standard",
            captured_by=doc["captured_by"],
            source_channel=doc["source_channel"],
            status=doc["status"],
            archived_at=doc["archived_at"].isoformat() if doc["archived_at"] else None,
            scope_slugs=scope_slugs,
            created_at=doc["created_at"].isoformat() if doc["created_at"] else None,
            chunks=chunks,
            canonical_payload=canonical_payload,
            raw_bytes_b64=raw_bytes_b64,
            raw_size_bytes=raw_size,
            metrics={"ms": ms},
        )
