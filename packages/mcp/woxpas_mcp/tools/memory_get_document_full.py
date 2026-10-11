"""memory_get_document_full — full document with all chunks + optional
decrypted canonical/raw payloads.

Direct fetch by doc_id. Works on archived documents (unlike memory_recall
which excludes them). Pillars: Speed (lazy decryption — DEK only loaded
when canonical/raw requested); Security (sensitivity-aware response).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import NotFound
from woxpas_mcp.registry import tool


class GetDocumentFullInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id:            UUID
    include_chunks:    bool = True
    include_canonical: bool = False
    include_raw_bytes: bool = False


@tool(
    name="memory_get_document_full",
    min_role="viewer",
    description=(
        "Get everything about one document: metadata, all chunks, and optionally the original content. Works on archived docs. Use when you need the complete source, not just a snippet."
        "decrypted canonical CDM payload + optional decrypted raw "
        "bytes.\n\n"
        "Works on archived docs (direct fetch by id; bypasses the "
        "retrieval-time archived filter). Use this when an agent needs "
        "the COMPLETE document, not just a preview.\n\n"
        "include_canonical=true returns structured CDM (text + structure). "
        "include_raw_bytes=true returns the original bytes b64-encoded — "
"rarely needed; opt-in only.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetDocumentFullInput,
)
async def memory_get_document_full(
    *, ctx: ToolContext, input: GetDocumentFullInput,
) -> dict[str, Any]:
    try:
        result = await ctx.document_read.get_document_full(
            tenant_id=ctx.tenant_id,
            doc_id=input.doc_id,
            include_chunks=input.include_chunks,
            include_canonical=input.include_canonical,
            include_raw_bytes=input.include_raw_bytes,
        )
    except ValueError as e:
        raise NotFound(str(e)) from e

    return {
        "doc_id":             str(result.doc_id),
        "title":              result.title,
        "filename":           result.filename,
        "summary":            result.summary,
        "topics":             result.topics,
        "processing_profile": result.processing_profile,
        "sensitivity_class":  result.sensitivity_class,
        "captured_by":        result.captured_by,
        "source_channel":     result.source_channel,
        "status":             result.status,
        "archived_at":        result.archived_at,
        "scope_slugs":        result.scope_slugs,
        "created_at":         result.created_at,
        "chunks": [
            {
                "chunk_id":    str(c.chunk_id),
                "ordinal":     c.ordinal,
                "text":        c.text,
                "token_count": c.token_count,
            }
            for c in result.chunks
        ],
        "canonical_payload": result.canonical_payload,
        "raw_bytes_b64":     result.raw_bytes_b64,
        "raw_size_bytes":    result.raw_size_bytes,
    }
