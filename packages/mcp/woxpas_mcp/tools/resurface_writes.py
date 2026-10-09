"""resurface_upsert_persona + resurface_record_decision —
Phase 5a.5+ agent-facing MCP tools.

Both are thin wrappers over engine methods that already exist.

Why these tools matter:
  - upsert_persona: an agent's style-learning skills need a clean
    write-path for tone profiles. Tone is BEHAVIORAL (lives in
    resurfacing layer's persona_tone jsonb), not factual (would have
    gone to KG).
  - record_decision: an agent's decision-style learning records
    every choice + later attaches user feedback. Sugar over
    record_observation with a stable context convention so future
    PatternDetector additions can mine it without schema changes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.registry import tool

# ─── resurface_upsert_persona ──────────────────────────────────────────────


class UpsertPersonaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_patch: dict[str, Any] = Field(default_factory=dict)
    """Convention:
      {
        'default': {'<channel>': {register, opener, sign_off, ...}},
        'per_relationship': {'<entity-uuid>': {'<channel>': {...}}}
      }
    Channels: 'email', 'slack', 'imessage', 'whatsapp', 'social', etc.
    Nested dicts merge recursively. Pass null for a key to drop it."""


@tool(
    name="resurface_upsert_persona",
    description=(
        "Store a behavioral persona/tone preference. Deep-merges into the user's persona profile — existing channels you don't touch stay intact. Read back via resurface_get_preferences."
        "BEHAVIORAL data — observed style/cadence/register per channel "
        "and (optionally) per relationship.\n\n"
        "Typical usage:\n"
        "  - on first connect to email: write {'default':{'email':{...}}}\n"
        "  - on per-relationship learning: write\n"
        "    {'per_relationship':{'<sarah-uuid>':{'email':{...}}}}\n\n"
        "The patch is deep-merged: existing channels you don't touch "
        "stay intact. Pass null to drop a key.\n\n"
        "Read back via resurface_get_preferences (returns the full "
"persona_tone field).\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=UpsertPersonaInput,
)
async def resurface_upsert_persona(
    *, ctx: ToolContext, input: UpsertPersonaInput,
) -> dict[str, Any]:
    return await ctx.resurface.upsert_persona(
        tenant_id=ctx.tenant_id,
        persona_patch=input.persona_patch,
    )


# ─── resurface_record_decision ─────────────────────────────────────────────


class RecordDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id:      str = Field(min_length=1, max_length=80)
    decision_kind: str = Field(min_length=1, max_length=120)
    """Free-form taxonomy. Examples: 'email_response_priority',
    'scope_assignment_choice', 'draft_tone_register'."""
    options: list[Any] = Field(default_factory=list)
    chosen:  Any = None
    why:     str | None = Field(default=None, max_length=2000)
    skill_id: str | None = Field(default=None, max_length=80)
    scope:    str | None = Field(default=None, max_length=120)


@tool(
    name="resurface_record_decision",
    description=(
        "Record a decision the user made with context. This feeds the pattern miner — over time Woxpas learns decision style and surfaces preferences automatically. Follow up with resurface_record_observation referencing the decision_id."
        "layer can mine for X.2 (decision-style learning).\n\n"
        "Returns {decision_id, observation_id} — both equal. an agent stores "
        "the id; when the user accepts/rejects the resulting action, "
        "follow up with resurface_record_observation pointing back to "
        "the decision_id in context.\n\n"
        "Sugar over record_observation with stable convention — no "
        "schema change required. Future PatternDetector additions will "
        "aggregate decisions by (decision_kind, chosen) and surface "
"preferences automatically.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=RecordDecisionInput,
)
async def resurface_record_decision(
    *, ctx: ToolContext, input: RecordDecisionInput,
) -> dict[str, Any]:
    return await ctx.resurface.record_decision(
        tenant_id=ctx.tenant_id,
        agent_id=input.agent_id,
        decision_kind=input.decision_kind,
        options=input.options,
        chosen=input.chosen,
        why=input.why,
        skill_id=input.skill_id,
        scope=input.scope,
    )
