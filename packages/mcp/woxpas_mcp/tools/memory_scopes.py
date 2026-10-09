"""memory_create_scope / assign_to_scope / unassign_from_scope.

Scope CRUD for the agent. Three-track scope model — Track 2:
agents calling memory_ingest from a conversation context should
provide `suggested_scope_slugs`. When no matching scope exists, the
agent calls memory_create_scope first, then memory_ingest with the
new slug. memory_assign_to_scope handles retroactive scoping;
memory_unassign_from_scope is the inverse for re-organization.

Pillars: Sovereign/Adaptable (scope is a recall signal — better
scoping = better retrieval quality across all tools).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import NotFound
from woxpas_mcp.registry import tool

# ─── memory_create_scope ────────────────────────────────────────────────────


class CreateScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name:        str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    slug:        str | None = Field(default=None, max_length=120)
    parent_scope_id: UUID | None = None
    color:       str | None = Field(default=None, max_length=20)
    icon:        str | None = Field(default=None, max_length=80)
    embed:       bool = True
    """Default True — embed name+description into Qdrant scopes_collection
    so subsequent ingests can auto-assign. Set False ONLY for catch-all
    scopes (like 'Inbox') that should never auto-claim docs."""


@tool(
    name="memory_create_scope",
    description=(
        "Create a new project, topic, or collection to organize documents. Use BEFORE memory_ingest when the user is discussing something new. Idempotent on slug — returns existing scope if one already exists with that slug."
        "name+description into Qdrant scopes_collection by default so "
        "subsequent memory_ingest calls can auto-assign matching docs.\n\n"
        "Use when ingesting an agent-saved memory from conversation and "
        "no existing scope matches the topic. List existing scopes via "
        "memory_recall(intent_hint='type_listing', query_text='scopes') "
        "first to avoid duplicate-named scopes.\n\n"
        "Idempotent on slug collision: returns the existing scope's "
"scope_id with already_existed=true.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=CreateScopeInput,
)
async def memory_create_scope(
    *, ctx: ToolContext, input: CreateScopeInput,
) -> dict[str, Any]:
    result = await ctx.scope_admin.create_scope(
        tenant_id=ctx.tenant_id,
        name=input.name,
        description=input.description,
        slug=input.slug,
        parent_scope_id=input.parent_scope_id,
        color=input.color,
        icon=input.icon,
        embed=input.embed,
    )
    return {
        "scope_id":        str(result.scope_id),
        "slug":            result.slug,
        "embedded":        result.embedded,
        "embedding_ref":   str(result.embedding_ref) if result.embedding_ref else None,
        "already_existed": result.already_existed,
    }


# ─── memory_assign_to_scope ─────────────────────────────────────────────────


class AssignToScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id:      UUID
    scope_slug:  str = Field(min_length=1, max_length=120)


@tool(
    name="memory_assign_to_scope",
    description=(
        "Add an existing document to a scope by slug. Use for retroactive organization. For new captures, prefer passing suggested_scope_slugs to memory_ingest at capture time."
        "returns inserted=False without error. Multi-scope membership "
        "allowed (a doc can belong to multiple scopes).\n\n"
        "Use for retroactive organization (existing doc + you realize "
        "which project it belongs to). For new captures, prefer "
"passing suggested_scope_slugs to memory_ingest at capture time.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=AssignToScopeInput,
)
async def memory_assign_to_scope(
    *, ctx: ToolContext, input: AssignToScopeInput,
) -> dict[str, Any]:
    try:
        result = await ctx.scope_admin.assign_to_scope(
            tenant_id=ctx.tenant_id,
            doc_id=input.doc_id,
            scope_slug=input.scope_slug,
            assigned_by="agent",
        )
    except ValueError as e:
        raise NotFound(str(e)) from e
    return {
        "scope_id": str(result.scope_id),
        "doc_id":   str(result.doc_id),
        "inserted": result.inserted,
    }


# ─── memory_unassign_from_scope ─────────────────────────────────────────────


class UnassignFromScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id:     UUID
    scope_slug: str = Field(min_length=1, max_length=120)


@tool(
    name="memory_unassign_from_scope",
    description=(
        "Remove a document from a scope. Idempotent. The doc may become unscoped — the dreamer will try to re-assign it."
        "non-existent assignment returns inserted=False (no-op).\n\n"
        "Three-track scope model: this is valid because unscoped docs "
        "are allowed (Track 1). After unassign, the doc may end up "
        "unscoped; the dreamer (Track 3a) will retry scope match on "
"the next cycle.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=UnassignFromScopeInput,
)
async def memory_unassign_from_scope(
    *, ctx: ToolContext, input: UnassignFromScopeInput,
) -> dict[str, Any]:
    try:
        result = await ctx.scope_admin.unassign_from_scope(
            tenant_id=ctx.tenant_id,
            doc_id=input.doc_id,
            scope_slug=input.scope_slug,
        )
    except ValueError as e:
        raise NotFound(str(e)) from e
    return {
        "scope_id": str(result.scope_id),
        "doc_id":   str(result.doc_id),
        "inserted": result.inserted,
    }
