"""resurface_* tools — Thinking Brain (context) + Dormant Brain (patterns).

Pure wrappers over ResurfaceEngine. Engines and HTTP routes are
already verified (5a.3.1 + 5a.3.2 + 5a.3.3 + 5a.3.4). This is plumbing.

Pillars:
  - Speed: every read is sub-100ms (engine optimized via score_maintainer
    + matview)
  - Sovereign/Adaptable: signals + preferences are tenant-portable —
    NOYU and 3rd-party MCP agents both consume the same surface
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.registry import tool

# ─── resurface_get_context (Thinking Brain) ─────────────────────────────────


class GetContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_entities: int = Field(default=20, ge=1, le=100)
    max_timeline: int = Field(default=25, ge=1, le=100)
    max_scopes:   int = Field(default=10, ge=1, le=50)


@tool(
    name="resurface_get_context",
    description=(
        "Call this ONCE at the start of every conversation. Returns "
        "everything you need to orient yourself: what the user is working "
        "on, what's overdue, what conflicts are open, and what entities "
        "are active right now.\n\n"
        "Returns: active scopes, ranked entities, recent timeline, open "
        "threads (overdue tasks + conflicts + developing ideas). Cheap "
"(~50ms). Use the result as background context for every turn.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetContextInput,
)
async def resurface_get_context(
    *, ctx: ToolContext, input: GetContextInput,
) -> dict[str, Any]:
    result = await ctx.resurface.thinking_context(
        tenant_id=ctx.tenant_id,
        max_entities=input.max_entities,
        max_timeline=input.max_timeline,
        max_scopes=input.max_scopes,
    )
    return result.model_dump(mode="json")


# ─── resurface_get_patterns (Dormant Brain) ─────────────────────────────────


class GetPatternsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kinds: list[str] | None = None
    """Filter by pattern kinds (any of: trending, fading, forgotten,
    recurring, attention). None = all kinds."""
    limit: int = Field(default=50, ge=1, le=200)
    recompute_if_empty: bool = True
    """When True (default), trigger sync PatternScorer.score_all if cache
    is empty (cold-start path). Subsequent calls hit warm cache."""


@tool(
    name="resurface_get_patterns",
    description=(
        "Call at session END or when the user asks 'what's changed?' or "
        "'anything I should know?'. Returns pattern signals the dreaming "
        "worker detected: trending topics, fading interests, forgotten "
        "ideas, recurring patterns, and attention signals (overdue tasks "
        "+ open conflicts).\n\n"
        "Pair with resurface_get_context: context = what's NOW, patterns "
"= what's CHANGED.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetPatternsInput,
)
async def resurface_get_patterns(
    *, ctx: ToolContext, input: GetPatternsInput,
) -> dict[str, Any]:
    result = await ctx.resurface.get_patterns(
        tenant_id=ctx.tenant_id,
        kinds=input.kinds,
        limit=input.limit,
        recompute_if_empty=input.recompute_if_empty,
    )
    return result.model_dump(mode="json")


# ─── resurface_record_observation ───────────────────────────────────────────


class RecordObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id:    str = Field(min_length=1, max_length=80)
    tool_called: str = Field(min_length=1, max_length=120)
    skill_id:    str | None = Field(default=None, max_length=80)
    tool_input:  dict[str, Any] | None = None
    outcome:     dict[str, Any] = Field(default_factory=dict)
    context:     dict[str, Any] = Field(default_factory=dict)


@tool(
    name="resurface_record_observation",
    description=(
        "Call this AFTER every tool invocation. Logs what you did and "
        "how it went so Woxpas builds cross-agent patterns over time. "
        "Cheap (~1ms), fire-and-forget — the dreaming worker mines these "
        "later for trends and preferences.\n\n"
        "Include agent_id, tool_called, outcome, and optional skill_id "
        "and context. This is how Woxpas learns which tools you use "
"together and what your workflow looks like."
    ),
    input_model=RecordObservationInput,
)
async def resurface_record_observation(
    *, ctx: ToolContext, input: RecordObservationInput,
) -> dict[str, Any]:
    from woxpas_resurfacing import ObservationRequest
    body = ObservationRequest(
        agent_id=input.agent_id,
        tool_called=input.tool_called,
        skill_id=input.skill_id,
        tool_input=input.tool_input,
        outcome=input.outcome,
        context=input.context,
    )
    await ctx.resurface.record_observation(tenant_id=ctx.tenant_id, body=body)
    return {"recorded": True}


# ─── resurface_get_preferences ──────────────────────────────────────────────


class GetPreferencesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@tool(
    name="resurface_get_preferences",
    description=(
        "Call ONCE at session start (right after resurface_get_context). "
        "Returns how this user likes answers: short vs detailed, citation "
        "density, tone, most-used tools, active hours. These preferences "
        "persist across all agents — switching from Claude to Cursor "
        "preserves the same behavior model.\n\n"
        "Use the result to shape your response style for every turn. "
"Cold tenants (new users) return sparse defaults.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetPreferencesInput,
)
async def resurface_get_preferences(
    *, ctx: ToolContext, input: GetPreferencesInput,
) -> dict[str, Any]:
    result = await ctx.resurface.get_preferences(tenant_id=ctx.tenant_id)
    return result.model_dump(mode="json")


# ─── resurface_get_tool_suggestions ─────────────────────────────────────────


class GetToolSuggestionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit:   int = Field(default=5, ge=1, le=20)
    context: dict[str, Any] | None = None


@tool(
    name="resurface_get_tool_suggestions",
    description=(
        "Which Woxpas tools this user calls most often, ranked by "
        "frequency over the last 30 days. Use to prioritize — if the "
        "user rarely uses scopes but often uses recall, don't suggest "
"scope operations unless asked.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetToolSuggestionsInput,
)
async def resurface_get_tool_suggestions(
    *, ctx: ToolContext, input: GetToolSuggestionsInput,
) -> dict[str, Any]:
    result = await ctx.resurface.get_tool_suggestions(
        tenant_id=ctx.tenant_id,
        context=input.context,
        limit=input.limit,
    )
    return result.model_dump(mode="json")
