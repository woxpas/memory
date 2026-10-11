"""resurface_* tools — predicted/prepared actions.

Memory-internal opportunities (predicted_action), pre-cached questions
(predicted_question), and the agent-posted prepared_action lifecycle.

Three-track scope model + Jarvis principle:
  - predicted_action: memory autonomously surfaces overdue tasks /
    open conflicts / dormant ideas / stale entities / unscoped docs
  - predicted_question: pre-cached synthesis answers (cloud-gated)
  - prepared_action: agent composes external-service actions, posts
    back to memory; any agent can list + walk through approval state

Pillars:
  - Sovereign/Adaptable: prepared_actions persist across agent swaps
  - Decide-don't-offer: action shapes are explicit (kind discriminator)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import InvalidState, NotFound
from woxpas_mcp.registry import tool

# ─── resurface_get_predicted_actions ────────────────────────────────────────


class GetPredictedActionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_kinds: list[str] | None = None
    """task_overdue | conflict_open | idea_dormant | entity_stale |
    doc_unscoped. None = all kinds."""
    subject_kind: str | None = None
    limit:        int = Field(default=50, ge=1, le=200)


@tool(
    name="resurface_get_predicted_actions",
    min_role="viewer",
    description=(
        "What needs attention. Returns overdue tasks, open conflicts, "
        "dormant ideas, and unscoped documents found by the dreaming "
        "worker. Each item has urgency (high/medium/low) and a "
        "suggested next step. Call when the user asks 'what should I "
"work on?' or to proactively surface forgotten items.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetPredictedActionsInput,
)
async def resurface_get_predicted_actions(
    *, ctx: ToolContext, input: GetPredictedActionsInput,
) -> dict[str, Any]:
    result = await ctx.resurface.list_predicted_actions(
        tenant_id=ctx.tenant_id,
        action_kinds=input.action_kinds,
        subject_kind=input.subject_kind,
        limit=input.limit,
    )
    return result.model_dump(mode="json")


# ─── resurface_get_predicted_questions ──────────────────────────────────────


class GetPredictedQuestionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_id: UUID | None = None
    limit:      int = Field(default=25, ge=1, le=100)


@tool(
    name="resurface_get_predicted_questions",
    min_role="viewer",
    description=(
        "Pre-cached answers from the dreaming worker. Empty when cloud "
        "is disallowed or nothing cached. Call at session start to check "
"for ready-made answers waiting for you.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=GetPredictedQuestionsInput,
)
async def resurface_get_predicted_questions(
    *, ctx: ToolContext, input: GetPredictedQuestionsInput,
) -> dict[str, Any]:
    result = await ctx.resurface.list_predicted_questions(
        tenant_id=ctx.tenant_id,
        subject_id=input.subject_id,
        limit=input.limit,
    )
    return result.model_dump(mode="json")


# ─── resurface_post_prepared_action ─────────────────────────────────────────


class PostPreparedActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id:           str = Field(min_length=1, max_length=80)
    action_kind:        str = Field(min_length=1, max_length=80)
    subject_label:      str | None = Field(default=None, max_length=500)
    subject_id:         UUID | None = None
    draft_content:      dict[str, Any] = Field(default_factory=dict)
    requires_approval:  bool = True
    expires_in_hours:   int = Field(default=72, ge=1, le=720)
    related_entity_ids: list[UUID] = Field(default_factory=list)


@tool(
    name="resurface_post_prepared_action",
    description=(
        "Save a draft action (email, message, calendar event) for "
        "cross-agent review. After you compose something, post it here "
        "so any agent can list, approve, and execute it later. Status "
        "starts as 'drafted'.\n\n"
        "After calling this tool, call resurface_record_observation "
        "to log the invocation. This builds your usage patterns — skip "
        "it and Woxpas stays cold."
    ),
    input_model=PostPreparedActionInput,
)
async def resurface_post_prepared_action(
    *, ctx: ToolContext, input: PostPreparedActionInput,
) -> dict[str, Any]:
    from woxpas_resurfacing import PreparedActionRequest
    body = PreparedActionRequest(
        agent_id=input.agent_id,
        action_kind=input.action_kind,
        subject_label=input.subject_label,
        subject_id=input.subject_id,
        draft_content=input.draft_content,
        requires_approval=input.requires_approval,
        expires_in_hours=input.expires_in_hours,
        related_entity_ids=input.related_entity_ids,
    )
    result = await ctx.resurface.post_prepared_action(
        tenant_id=ctx.tenant_id, body=body,
    )
    return result.model_dump(mode="json")


# ─── resurface_list_prepared_actions ────────────────────────────────────────


class ListPreparedActionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statuses:    list[str] | None = None
    """Default: drafted+approved (still actionable). Pass full list for
    history."""
    agent_id:    str | None = Field(default=None, max_length=80)
    action_kind: str | None = Field(default=None, max_length=80)
    expiring_within_hours: int | None = Field(default=None, ge=1, le=720)
    limit:       int = Field(default=50, ge=1, le=200)


@tool(
    name="resurface_list_prepared_actions",
    min_role="viewer",
    description=(
        "List pending actions across all agents. Filter by status (drafted/approved/executed/rejected), agent, or kind. Default shows drafted + approved — still actionable. Use to surface what's waiting for review or execution."
        "(still actionable). Filter by agent_id, action_kind, or "
        "expiring_within_hours.\n\n"
        "Pair with resurface_approve_prepared_action / reject / "
"mark_executed to walk through the approval state machine.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=ListPreparedActionsInput,
)
async def resurface_list_prepared_actions(
    *, ctx: ToolContext, input: ListPreparedActionsInput,
) -> dict[str, Any]:
    result = await ctx.resurface.list_prepared_actions(
        tenant_id=ctx.tenant_id,
        statuses=input.statuses,
        agent_id=input.agent_id,
        action_kind=input.action_kind,
        expiring_within_hours=input.expiring_within_hours,
        limit=input.limit,
    )
    return result.model_dump(mode="json")


# ─── resurface_approve_prepared_action ──────────────────────────────────────


class ApprovePreparedActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID


@tool(
    name="resurface_approve_prepared_action",
    description=(
        "Approve a drafted action so it can be executed. drafted → approved. Idempotent on already-approved."
        "current state). Errors with invalid_state if the artifact is "
"executed/rejected/expired (TTL-aware).\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=ApprovePreparedActionInput,
)
async def resurface_approve_prepared_action(
    *, ctx: ToolContext, input: ApprovePreparedActionInput,
) -> dict[str, Any]:
    try:
        result = await ctx.resurface.approve_prepared_action(
            tenant_id=ctx.tenant_id, artifact_id=input.artifact_id,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg or "TTL" in msg:
            raise NotFound(msg) from e
        raise InvalidState(msg) from e
    return result.model_dump(mode="json")


# ─── resurface_reject_prepared_action ───────────────────────────────────────


class RejectPreparedActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID
    reason:      str | None = Field(default=None, max_length=500)


@tool(
    name="resurface_reject_prepared_action",
    description=(
        "Reject an action. drafted/approved → rejected. Provide a reason so the user knows why."
"TTL-expired artifacts cannot be transitioned.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=RejectPreparedActionInput,
)
async def resurface_reject_prepared_action(
    *, ctx: ToolContext, input: RejectPreparedActionInput,
) -> dict[str, Any]:
    try:
        result = await ctx.resurface.reject_prepared_action(
            tenant_id=ctx.tenant_id, artifact_id=input.artifact_id,
            reason=input.reason,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg or "TTL" in msg:
            raise NotFound(msg) from e
        raise InvalidState(msg) from e
    return result.model_dump(mode="json")


# ─── resurface_mark_executed ────────────────────────────────────────────────


class MarkExecutedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID


@tool(
    name="resurface_mark_executed",
    description=(
        "Mark an approved action as done. Call after you've executed the action via your own connector (e.g., sent the email). Records the receipt for audit. approved → executed."
        "action via its own connector (e.g., Gmail send, Slack post). "
"Memory persists the receipt for audit + dedupe.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=MarkExecutedInput,
)
async def resurface_mark_executed(
    *, ctx: ToolContext, input: MarkExecutedInput,
) -> dict[str, Any]:
    try:
        result = await ctx.resurface.mark_prepared_executed(
            tenant_id=ctx.tenant_id, artifact_id=input.artifact_id,
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg or "TTL" in msg:
            raise NotFound(msg) from e
        raise InvalidState(msg) from e
    return result.model_dump(mode="json")
