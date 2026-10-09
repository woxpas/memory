"""memory_list_conflicts / get_conflict / resolve_conflict.

Conflicts aren't in /recall's Intent enum — they have their own surface.
Resolution honors the four shapes from project_conflict_resolution_shapes.md:

  pick         — winner + loser (one correct, the other rejected)
  coexistence  — discriminators (both correct in different contexts)
  progression  — valid_times (temporal evolution A → B → C)
  dismissed    — false-positive or "agree to disagree"

Pillars: Accuracy (KG quality depends on resolved conflicts);
Decide-don't-offer (the 4 shapes are exhaustive — agent picks one).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import InvalidState, NotFound, ValidationFailed
from woxpas_mcp.registry import tool

# ─── memory_list_conflicts ──────────────────────────────────────────────────


class ListConflictsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state:    Literal["open", "auto_resolved", "human_resolved", "living", "dismissed", "all"] = "open"
    severity: Literal["critical", "high", "medium", "low"] | None = None
    kind:     str | None = Field(default=None, max_length=80)
    limit:    int = Field(default=50, ge=1, le=200)


@tool(
    name="memory_list_conflicts",
    description=(
        "List contradictions in stored knowledge. Default shows open conflicts ordered by severity. Call when the user asks 'are there contradictions in what I know?' or proactively when you spot something that conflicts with prior knowledge."
        "by severity (critical first), then detected_at desc. Pass "
        "state='all' to see resolved history.\n\n"
        "Conflicts are detected by the pipeline when contradictory facts "
        "land in the KG. Use memory_get_conflict for full detail and "
"memory_resolve_conflict to apply a resolution.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=ListConflictsInput,
)
async def memory_list_conflicts(
    *, ctx: ToolContext, input: ListConflictsInput,
) -> dict[str, Any]:
    items = await ctx.conflict.list_conflicts(
        tenant_id=ctx.tenant_id,
        state=input.state, severity=input.severity,
        kind=input.kind, limit=input.limit,
    )
    return {
        "items": [
            {
                "conflict_id":        str(i.conflict_id),
                "kind":               i.kind,
                "severity":           i.severity,
                "predicate":          i.predicate,
                "notes":              i.notes,
                "detected_at":        i.detected_at,
                "subject_entity_ids": [str(eid) for eid in i.subject_entity_ids],
                "doc_refs":           [str(d) for d in i.doc_refs],
            }
            for i in items
        ],
        "total": len(items),
    }


# ─── memory_get_conflict ────────────────────────────────────────────────────


class GetConflictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflict_id: UUID


@tool(
    name="memory_get_conflict",
    description=(
        "Full detail on one conflict: both sides, source documents, timestamps, and suggested resolution shapes (pick one, coexistence, progression, or dismissed)."
"with chunk provenance) and suggested resolution shapes hint.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetConflictInput,
)
async def memory_get_conflict(
    *, ctx: ToolContext, input: GetConflictInput,
) -> dict[str, Any]:
    try:
        d = await ctx.conflict.get_conflict(
            tenant_id=ctx.tenant_id, conflict_id=input.conflict_id,
        )
    except ValueError as e:
        raise NotFound(str(e)) from e

    return {
        "conflict_id":        str(d.conflict_id),
        "kind":               d.kind,
        "severity":           d.severity,
        "predicate":          d.predicate,
        "notes":              d.notes,
        "detected_at":        d.detected_at,
        "resolved_state":     d.resolved_state,
        "resolved_value":     d.resolved_value,
        "resolved_at":        d.resolved_at,
        "subject_entity_ids": [str(eid) for eid in d.subject_entity_ids],
        "doc_refs":           [str(dr) for dr in d.doc_refs],
        "mention_refs":       d.mention_refs,
        "suggested_shapes":   d.suggested_shapes,
    }


# ─── memory_resolve_conflict ────────────────────────────────────────────────


class ResolveConflictInput(BaseModel):
    """Discriminated by `kind`. Required-field semantics:
      pick:        winner + loser
      coexistence: discriminators (e.g., {'context':'home_address'})
      progression: valid_times (e.g., {'old':'2020..2022','new':'2022..'})
      dismissed:   no extra fields required; note recommended
    """
    model_config = ConfigDict(extra="forbid")

    conflict_id:    UUID
    kind:           Literal["pick", "coexistence", "progression", "dismissed"]
    winner:         UUID | None = None
    loser:          UUID | None = None
    discriminators: dict[str, str] | None = None
    valid_times:    dict[str, str] | None = None
    note:           str | None = Field(default=None, max_length=2000)


@tool(
    name="memory_resolve_conflict",
    description=(
        "Resolve a conflict. Shapes: pick (choose one winner), coexistence (both true in different contexts), progression (newer supersedes older), dismissed (false detection). Resolution is recorded permanently."
        "resolved_state.\n\n"
        "Shapes (per project_conflict_resolution_shapes.md):\n"
        "  pick — one value correct, the other rejected. Requires "
        "winner + loser (claim or entity ids).\n"
        "  coexistence — both correct in different contexts. Requires "
        "discriminators (e.g., context, time_window, location).\n"
        "  progression — temporal evolution. Requires valid_times for "
        "each side.\n"
        "  dismissed — false-positive detection or agree-to-disagree. "
        "No required fields; note recommended.\n\n"
        "Re-resolving an already-resolved conflict returns "
"invalid_state in v0.1 (no resolution editing yet).\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=ResolveConflictInput,
)
async def memory_resolve_conflict(
    *, ctx: ToolContext, input: ResolveConflictInput,
) -> dict[str, Any]:
    try:
        result = await ctx.conflict.resolve_conflict(
            tenant_id=ctx.tenant_id,
            conflict_id=input.conflict_id,
            kind=input.kind,
            winner=input.winner, loser=input.loser,
            discriminators=input.discriminators,
            valid_times=input.valid_times,
            note=input.note,
            resolved_by="agent",
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise NotFound(msg) from e
        if "already resolved" in msg:
            raise InvalidState(msg) from e
        raise ValidationFailed(msg) from e

    return {
        "conflict_id": str(result.conflict_id),
        "kind":        result.kind,
        "new_state":   result.new_state,
    }
