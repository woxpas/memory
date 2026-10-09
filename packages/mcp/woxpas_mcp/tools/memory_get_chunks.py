"""memory_get_chunks — hydrate chunk_ids to full text.

Used by agents that received a synthesis answer with the LEAN citation
envelope (chunk_ids only, no inline text) and need the full text to
quote verbatim or elaborate.

Tenant-scoped — cross-tenant chunk_ids are silently dropped (don't leak
existence across tenants, Pillar 3).

Pillars: Speed (batch hydration in one tool call); Security (tenant
boundary enforced at the engine layer).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.registry import tool


class GetChunksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_ids: list[UUID] = Field(min_length=1, max_length=100)
    """One or more chunk_ids to hydrate. Cap 100 per call."""

    context_window: int = Field(default=0, ge=0, le=5)
    """When > 0, include N surrounding chunks (prev + next) per target.
    Default 0 (target chunks only). Caller pays for context in token
    budget — opt in only when needed for elaboration."""


@tool(
    name="memory_get_chunks",
    description=(
        "Get full text for one or more chunks by ID. Use after memory_recall or memory_ask returned chunk IDs — this hydrates them to complete text. Batch up to 100 at once. Set context_window > 0 to get surrounding chunks for context."
        "memory_recall or memory_ask returned chunk_ids in a lean "
        "envelope. Tenant-isolated — chunk_ids belonging to other "
        "tenants are silently dropped (Pillar 3 security boundary).\n\n"
        "Pass context_window>0 only when you need surrounding chunks "
"for elaboration; default 0 keeps responses tight.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetChunksInput,
)
async def memory_get_chunks(
    *, ctx: ToolContext, input: GetChunksInput,
) -> dict[str, Any]:
    items = await ctx.retrieval.recall_chunks_by_ids(
        tenant_id=ctx.tenant_id,
        chunk_ids=list(input.chunk_ids),
    )
    out: list[dict[str, Any]] = []
    for it in items:
        out.append({
            "chunk_id":          str(it.id),
            "doc_id":            str((it.metadata or {}).get("doc_id") or ""),
            "ordinal":           (it.metadata or {}).get("ordinal"),
            "text":              (it.metadata or {}).get("text") or it.snippet,
            "title":             it.title,
            "sensitivity_class": it.sensitivity_class,
        })

    # Optional: context_window — fetch prev/next per target chunk
    # via chunk_with_context. Skip when 0 (default) to keep responses lean.
    contexts: list[dict[str, Any]] = []
    if input.context_window > 0:
        for cid in input.chunk_ids:
            ctx_result = await ctx.retrieval.recall_chunk_with_context(
                chunk_id=cid,
                tenant_id=ctx.tenant_id,
                context_window=input.context_window,
            )
            if ctx_result is not None:
                contexts.append(ctx_result.model_dump(mode="json")
                                if hasattr(ctx_result, "model_dump")
                                else dict(ctx_result))

    return {
        "chunks":   out,
        "contexts": contexts,
        "returned": len(out),
        "requested": len(input.chunk_ids),
    }
