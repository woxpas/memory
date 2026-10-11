"""memory_recall — retrieve chunks for a query.

Wraps the existing /recall HTTP endpoint's underlying RetrievalEngine
call. Covers semantic + entity_card + type_listing + file_lookup +
graph + timeline via the intent_hint parameter (the Intent enum).

Best practice for agents:
  - Use intent_hint='semantic' for free-text queries
  - Use intent_hint='entity_card' when you have an entity name/id
  - Use intent_hint='type_listing' for "list all my tasks/ideas/files"
  - Use intent_hint='file_lookup' to fetch a doc by id/filename
  - Use intent_hint='graph' for relation traversal from a seed entity
  - Use intent_hint='auto' to let the rule classifier + LLM fallback decide

Returns a lean envelope (chunk_ids + snippets); agents fetch full text
via memory_get_chunks when needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.registry import tool

# ─── Input model — JSON Schema generated automatically ──────────────────────


class TimeFilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since:    datetime | None = None
    until:    datetime | None = None
    relative: str | None = None
    """E.g., 'last_hour', 'last_day', 'last_week', 'today', 'this_month'."""


class FiltersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_slugs:         list[str] = Field(default_factory=list)
    time:                TimeFilterInput | None = None
    sensitivity:         Literal["any", "exclude_sensitive", "only_sensitive"] = "any"
    processing_profiles: list[str] = Field(default_factory=list)
    entity_ids:          list[UUID] = Field(default_factory=list)
    predicate_kinds:     list[str] = Field(default_factory=list)
    include_archived:    bool = False


class RecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_text: str = Field(min_length=1, max_length=2000)
    """Natural-language query (semantic) or structured ID for lookups
    ('doc:<uuid>', 'entity:<uuid>', or quoted name)."""

    intent_hint: Literal[
        "auto", "semantic", "timeline", "type_listing",
        "file_lookup", "entity_card", "graph",
    ] = "auto"

    filters:   FiltersInput | None = None
    limit:     int = Field(default=20, ge=1, le=200)
    rerank:    bool = True
    enrich_with_entities: bool = True
    """Phase 4.9 — auto-detect entities in query + pull rich context."""


# ─── Handler ────────────────────────────────────────────────────────────────


@tool(
    name="memory_recall",
    min_role="viewer",
    description=(
        "Search your stored memory for relevant context. Use this when you "
        "need raw information to inform your thinking — not a polished "
        "answer for the user.\n\n"
        "When to use memory_recall vs memory_ask:\n"
        "  memory_recall → you need context, snippets, facts to work with\n"
        "  memory_ask    → the user wants a direct answer with citations\n\n"
        "Intent routing (intent_hint):\n"
        "- semantic (default): hybrid vector + keyword search — best for "
        "  'what do I know about X?' queries\n"
        "- entity_card: full profile of a person/company/thing. Use this "
        "when the user asks 'who am I?', 'what do you know about me?', "
        "or 'tell me about [name]'. Returns profile facts, relations, "
        "tasks, and ideas aggregated from everything ever ingested about "
        "that entity. Much richer than semantic search for identity queries.\n"
        "- type_listing: list all ideas, tasks, files, or scopes\n"
        "- file_lookup: find one document by name or id\n"
        "- graph: how entities relate to each other\n"
        "- timeline: what happened in a time window\n"
        "- auto: let the system decide (default if you're unsure)\n"
        "- auto: rule classifier + LLM fallback decides\n\n"
        "Returns lean envelope (chunk_id + snippet metadata). Use "
        "memory_get_chunks for full text. Archived docs are excluded; "
"use memory_get_document_full to fetch by id.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=RecallInput,
)
async def memory_recall(*, ctx: ToolContext, input: RecallInput) -> dict[str, Any]:
    from woxpas_retrieval import RetrievalRequest
    from woxpas_retrieval.types import (
        Filters as RetrFilters,
    )
    from woxpas_retrieval.types import (
        Intent,
        SensitivityFilter,
    )
    from woxpas_retrieval.types import (
        TimeFilter as RetrTimeFilter,
    )

    intent = Intent(input.intent_hint)
    sensitivity = SensitivityFilter(input.filters.sensitivity) if input.filters else SensitivityFilter.ANY

    time_f = None
    if input.filters and input.filters.time:
        t = input.filters.time
        time_f = RetrTimeFilter(since=t.since, until=t.until, relative=t.relative)

    filters = RetrFilters(
        scope_slugs=input.filters.scope_slugs if input.filters else [],
        time=time_f,
        sensitivity=sensitivity,
        processing_profiles=input.filters.processing_profiles if input.filters else [],
        entity_ids=input.filters.entity_ids if input.filters else [],
        predicate_kinds=input.filters.predicate_kinds if input.filters else [],
        include_archived=input.filters.include_archived if input.filters else False,
    )

    req = RetrievalRequest(
        query_text=input.query_text,
        intent_hint=intent,
        filters=filters,
        limit=input.limit,
        rerank=input.rerank,
        enrich_with_entities=input.enrich_with_entities,
    )

    result = await ctx.retrieval.recall(req, tenant_id=ctx.tenant_id)
    # Pydantic model_dump for stable JSON shape
    return result.model_dump(mode="json")
