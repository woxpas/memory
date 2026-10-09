"""memory_archive_document / unarchive_document.

Archive removes a document from retrieval (semantic + timeline + type-
listing all exclude `archived_at IS NOT NULL`). Direct fetch by id
(memory_get_document_full, memory_get_chunks) still serves archived
docs so unarchive flow + pinned-chunk continuity work.

Hard delete is intentionally NOT exposed in v0.1 — archive is the
user-facing 'remove from view' affordance.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import NotFound
from woxpas_mcp.registry import tool


class ArchiveDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: UUID
    reason: str | None = Field(default=None, max_length=500)


@tool(
    name="memory_archive_document",
    description=(
        "Hide a document from search results. Entities and facts remain intact — the doc is hidden, not deleted. Use for outdated or superseded content. Reversible."
        "(state_changed=False).\n\n"
        "Effect: archived docs are excluded from memory_recall (all "
        "intents) and the doc_unscoped opportunity surface. Direct "
        "fetch by id (memory_get_document_full, memory_get_chunks) "
        "still serves archived docs so unarchive flow + pinned-chunk "
        "continuity work.\n\n"
        "KG entities and relations remain intact (history-preserving). "
"To restore, call memory_unarchive_document.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=ArchiveDocumentInput,
)
async def memory_archive_document(
    *, ctx: ToolContext, input: ArchiveDocumentInput,
) -> dict[str, Any]:
    try:
        result = await ctx.document_admin.archive_document(
            tenant_id=ctx.tenant_id, doc_id=input.doc_id, reason=input.reason,
        )
    except ValueError as e:
        raise NotFound(str(e)) from e
    return {
        "doc_id":        str(result.doc_id),
        "state_changed": result.state_changed,
        "archived_at":   result.archived_at,
    }


class UnarchiveDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: UUID


@tool(
    name="memory_unarchive_document",
    description=(
        "Restore an archived document to search results. Idempotent — no-op if already live."
"live). Doc reappears in memory_recall results immediately.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=UnarchiveDocumentInput,
)
async def memory_unarchive_document(
    *, ctx: ToolContext, input: UnarchiveDocumentInput,
) -> dict[str, Any]:
    try:
        result = await ctx.document_admin.unarchive_document(
            tenant_id=ctx.tenant_id, doc_id=input.doc_id,
        )
    except ValueError as e:
        raise NotFound(str(e)) from e
    return {
        "doc_id":        str(result.doc_id),
        "state_changed": result.state_changed,
        "archived_at":   result.archived_at,
    }
