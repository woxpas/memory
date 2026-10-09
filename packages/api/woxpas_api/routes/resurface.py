"""Resurfacing endpoints — cognition layer for any plug-in agent.

Phase 5a.1 ships GET /resurface/context (the Thinking Brain). Future
phases:
  5a.2 → /resurface/patterns (Dormant Brain — trending/fading/forgotten)
  5a.4 → POST /resurface/observation + GET /resurface/tools
         (Tool-Use Pattern engine)
  5a.5 → MCP server wraps all of the above for any MCP-capable agent

The contract: tenant_id is from auth (never request body); response is
JSON; surface is plug-and-play across NOYU + 3rd-party agents.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from woxpas_resurfacing import (
    ObservationRequest,
    PatternsResult,
    PredictedActionListResponse,
    PredictedQuestionListResponse,
    PreparedActionItem,
    PreparedActionListResponse,
    PreparedActionPostResponse,
    PreparedActionRejectRequest,
    PreparedActionRequest,
    ThinkingBrainContext,
    ToolSuggestions,
    UserPreferences,
)

from woxpas_api.auth import tenant_id_dep
from woxpas_api.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(tags=["resurface"])


def _get_state(request: Request) -> AppState:
    return request.app.state.app


@router.get("/resurface/context", response_model=ThinkingBrainContext)
async def resurface_context(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    max_entities: Annotated[int, Query(ge=1, le=100)] = 20,
    max_timeline: Annotated[int, Query(ge=1, le=100)] = 25,
    max_scopes:   Annotated[int, Query(ge=1, le=50)]  = 10,
) -> ThinkingBrainContext:
    """Thinking Brain — situational snapshot of what's live in memory.

    Agent flow: call once at session start (or every N seconds for long
    sessions), pass to LLM as background frame. Cheap query (~50-100ms
    on a typical tenant); cache client-side ~60s for repeated requests.

    Returns:
      - now / day_of_week / time_of_day / timezone (server-computed)
      - active_scopes (touched in last 7 days)
      - active_entities (mentioned in last 7 days, ordered by 24h velocity)
      - recent_timeline (last 25 events)
      - open_threads (tasks_due_soon + overdue, ideas_developing,
                      conflicts_open)
      - session_signals (recent_docs from last 24h; queries + tools
                         populate after Phase 5a.4 ships)

    Tenant-isolated: tenant_id from auth, RLS-enforced via tenant_session.
    """
    state = _get_state(request)
    try:
        return await state.resurface.thinking_context(
            tenant_id=tenant_id,
            max_entities=max_entities,
            max_timeline=max_timeline,
            max_scopes=max_scopes,
        )
    except Exception as e:
        log.error("resurface.context.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.context failed: {type(e).__name__}",
        ) from e


@router.post("/resurface/refresh-scores")
async def resurface_refresh_scores(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> dict:
    """Manually trigger ScoreMaintainer for this tenant.

    Walks active entities/ideas/documents and refreshes
    surface_score / decay_score / momentum_score columns. Phase 5a.3
    will run this on a schedule; for now (5a.2.1), call manually after
    significant ingest activity to keep priority signals fresh.

    Returns per-class row counts + total time. Sub-second for typical
    tenants; scales linearly with active rows in the 90-day window.
    """
    state = _get_state(request)
    try:
        return await state.resurface.refresh_scores(tenant_id=tenant_id)
    except Exception as e:
        log.error("resurface.refresh_scores.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.refresh_scores failed: {type(e).__name__}",
        ) from e


@router.post("/resurface/observation", status_code=status.HTTP_204_NO_CONTENT)
async def resurface_observation(
    body:      ObservationRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> None:
    """Record one agent interaction. Plug-and-play — any agent (NOYU
    or 3rd-party MCP) calls this on every tool use.

    The body carries: agent_id, tool_called, tool_input (optional),
    outcome (success/latency/user_engaged), context (time/scope/etc).
    Resurfacing aggregates these into cross-agent user preferences
    via PatternDetector (runs in the dreaming worker on a 30-min
    cadence).

    Cheap write (~1ms). Tenant-isolated. Returns 204 — agents don't
    need to wait for analysis."""
    state = _get_state(request)
    try:
        await state.resurface.record_observation(tenant_id=tenant_id, body=body)
    except Exception as e:
        log.error("resurface.observation.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.observation failed: {type(e).__name__}",
        ) from e


@router.get("/resurface/preferences", response_model=UserPreferences)
async def resurface_preferences(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> UserPreferences:
    """Cross-agent user preferences derived from observations. Any
    agent reads here on every conversational turn to shape its
    response (length, tone, citation density). Falls back to defaults
    when the tenant is cold (no observations yet).

    `observation_count_30d` tells the agent how warm the preferences
    are — < 20 means treat preferences as suggestive only."""
    state = _get_state(request)
    try:
        return await state.resurface.get_preferences(tenant_id=tenant_id)
    except Exception as e:
        log.error("resurface.preferences.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.preferences failed: {type(e).__name__}",
        ) from e


@router.get("/resurface/tools", response_model=ToolSuggestions)
async def resurface_tools(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    limit:     Annotated[int, Query(ge=1, le=20)] = 5,
) -> ToolSuggestions:
    """Frequency-ranked tool suggestions. v0.1: pure frequency over
    last 30 days. v0.2: context-aware sequencing (A→B chains)."""
    state = _get_state(request)
    try:
        return await state.resurface.get_tool_suggestions(
            tenant_id=tenant_id, limit=limit,
        )
    except Exception as e:
        log.error("resurface.tools.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.tools failed: {type(e).__name__}",
        ) from e


@router.get("/resurface/predicted-actions", response_model=PredictedActionListResponse)
async def resurface_predicted_actions(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    action_kinds: Annotated[
        list[str] | None,
        Query(description="Filter by action_kind (e.g. task_overdue, "
                          "conflict_open, idea_dormant, entity_stale, "
                          "doc_unscoped). Omit for all kinds."),
    ] = None,
    subject_kind: Annotated[str | None, Query()] = None,
    limit:        Annotated[int, Query(ge=1, le=200)] = 50,
) -> PredictedActionListResponse:
    """Memory-internal action opportunities — what the dreaming worker
    has noticed about the user's CAPTURED knowledge graph.

    Cold-start fallback: if cache is empty, runs OpportunityDetector
    synchronously before serving (one-time cost).

    Tenant-isolated. Suggested action lists carry reversibility +
    requires_approval HINTS for agent UI; memory does not enforce."""
    state = _get_state(request)
    try:
        return await state.resurface.list_predicted_actions(
            tenant_id=tenant_id,
            action_kinds=action_kinds,
            subject_kind=subject_kind,
            limit=limit,
        )
    except Exception as e:
        log.error("resurface.predicted_actions.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.predicted_actions failed: {type(e).__name__}",
        ) from e


@router.get("/resurface/predicted-questions", response_model=PredictedQuestionListResponse)
async def resurface_predicted_questions(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    subject_id: Annotated[UUID | None, Query()] = None,
    limit:      Annotated[int, Query(ge=1, le=100)] = 25,
) -> PredictedQuestionListResponse:
    """Pre-cached synthesis answers from QuestionPreCacher (5a.3.1).
    Empty when no entries cached for this tenant yet OR when cloud is
    disallowed and the worker has nothing local to compute."""
    state = _get_state(request)
    try:
        return await state.resurface.list_predicted_questions(
            tenant_id=tenant_id, subject_id=subject_id, limit=limit,
        )
    except Exception as e:
        log.error("resurface.predicted_questions.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.predicted_questions failed: {type(e).__name__}",
        ) from e


@router.post("/resurface/prepared-action", response_model=PreparedActionPostResponse,
             status_code=status.HTTP_201_CREATED)
async def resurface_prepared_action_post(
    body:      PreparedActionRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> PreparedActionPostResponse:
    """Agent posts a composed action back to memory. Memory stores it
    as a prepared_action artifact in 'drafted' state with the supplied
    expiration. Any agent (NOYU + 3rd-party MCP) can list these and
    walk them through the approval state machine."""
    state = _get_state(request)
    try:
        return await state.resurface.post_prepared_action(
            tenant_id=tenant_id, body=body,
        )
    except Exception as e:
        log.error("resurface.prepared_action.post.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.prepared_action.post failed: {type(e).__name__}",
        ) from e


@router.get("/resurface/prepared-actions", response_model=PreparedActionListResponse)
async def resurface_prepared_actions(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    statuses:    Annotated[
        list[str] | None,
        Query(description="Filter by status (drafted, approved, executed, "
                          "rejected, expired). Default: drafted+approved."),
    ] = None,
    agent_id:    Annotated[str | None, Query()] = None,
    action_kind: Annotated[str | None, Query()] = None,
    expiring_within_hours: Annotated[int | None, Query(ge=1, le=720)] = None,
    limit:       Annotated[int, Query(ge=1, le=200)] = 50,
) -> PreparedActionListResponse:
    """List prepared_action artifacts. Default returns only still-
    actionable rows (drafted + approved); pass statuses to override."""
    state = _get_state(request)
    try:
        return await state.resurface.list_prepared_actions(
            tenant_id=tenant_id, statuses=statuses, agent_id=agent_id,
            action_kind=action_kind,
            expiring_within_hours=expiring_within_hours,
            limit=limit,
        )
    except Exception as e:
        log.error("resurface.prepared_actions.list.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.prepared_actions.list failed: {type(e).__name__}",
        ) from e


@router.post("/resurface/prepared-action/{artifact_id}/approve",
             response_model=PreparedActionItem)
async def resurface_prepared_action_approve(
    artifact_id: UUID,
    request:     Request,
    tenant_id:   Annotated[UUID, Depends(tenant_id_dep)],
) -> PreparedActionItem:
    """Move a prepared_action from drafted → approved. Idempotent on
    already-approved; errors with 409 if the artifact is in an
    incompatible state (executed, rejected, expired)."""
    state = _get_state(request)
    try:
        return await state.resurface.approve_prepared_action(
            tenant_id=tenant_id, artifact_id=artifact_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e),
        ) from e
    except Exception as e:
        log.error("resurface.prepared_action.approve.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.prepared_action.approve failed: {type(e).__name__}",
        ) from e


@router.post("/resurface/prepared-action/{artifact_id}/reject",
             response_model=PreparedActionItem)
async def resurface_prepared_action_reject(
    artifact_id: UUID,
    body:        PreparedActionRejectRequest,
    request:     Request,
    tenant_id:   Annotated[UUID, Depends(tenant_id_dep)],
) -> PreparedActionItem:
    """drafted | approved → rejected. Captures optional reason."""
    state = _get_state(request)
    try:
        return await state.resurface.reject_prepared_action(
            tenant_id=tenant_id, artifact_id=artifact_id,
            reason=body.reason,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e),
        ) from e
    except Exception as e:
        log.error("resurface.prepared_action.reject.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.prepared_action.reject failed: {type(e).__name__}",
        ) from e


@router.post("/resurface/prepared-action/{artifact_id}/mark-executed",
             response_model=PreparedActionItem)
async def resurface_prepared_action_mark_executed(
    artifact_id: UUID,
    request:     Request,
    tenant_id:   Annotated[UUID, Depends(tenant_id_dep)],
) -> PreparedActionItem:
    """approved → executed. Agent posts back after running the action
    via its own connector. Memory persists the receipt for audit +
    dedupe."""
    state = _get_state(request)
    try:
        return await state.resurface.mark_prepared_executed(
            tenant_id=tenant_id, artifact_id=artifact_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e),
        ) from e
    except Exception as e:
        log.error("resurface.prepared_action.execute.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.prepared_action.execute failed: {type(e).__name__}",
        ) from e


# ─── Phase 5a.5+ — NOYU-supporting writes ──────────────────────────────


class UpsertPersonaRequest(BaseModel):
    """Body for POST /resurface/persona — deep-merge a persona patch into
    resurface_user_preferences.persona_tone."""
    model_config = ConfigDict(extra="forbid")

    persona_patch: dict[str, Any] = Field(default_factory=dict)
    """Top-level keys: 'default' (channel-keyed defaults), 'per_relationship'
    (entity-id-keyed overrides). Nested dicts merge recursively. Pass null
    for a key to drop it."""


class UpsertPersonaResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    persona_tone: dict[str, Any] = Field(default_factory=dict)


@router.post("/resurface/persona", response_model=UpsertPersonaResponse)
async def resurface_upsert_persona(
    body:      UpsertPersonaRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> UpsertPersonaResponse:
    """Deep-merge persona patch into the tenant's persona_tone.

    Tone profile is BEHAVIORAL data (observed style/cadence/register per
    channel + per relationship). Lives in resurfacing layer, not the KG.
    Use this when an agent learns the user's writing style and wants
    future drafts to be informed by it.

    Convention:
      persona_tone = {
        'default': {'<channel>': {register, opener, sign_off, avg_words, ...}},
        'per_relationship': {'<entity-uuid>': {'<channel>': {...}}}
      }
    """
    state = _get_state(request)
    try:
        result = await state.resurface.upsert_persona(
            tenant_id=tenant_id, persona_patch=body.persona_patch,
        )
        return UpsertPersonaResponse(persona_tone=result.get("persona_tone", {}))
    except Exception as e:
        log.error("resurface.upsert_persona.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.upsert_persona failed: {type(e).__name__}",
        ) from e


class RecordDecisionRequest(BaseModel):
    """Body for POST /resurface/decision — record an agent decision."""
    model_config = ConfigDict(extra="forbid")

    agent_id:      str = Field(min_length=1, max_length=80)
    decision_kind: str = Field(min_length=1, max_length=120)
    """Free-form taxonomy — agents agree informally. Examples:
    'email_response_priority', 'scope_assignment_choice',
    'draft_tone_register', 'tool_chain_selection'."""
    options:       list[Any] = Field(default_factory=list)
    chosen:        Any = None
    why:           str | None = Field(default=None, max_length=2000)
    skill_id:      str | None = Field(default=None, max_length=80)
    scope:         str | None = Field(default=None, max_length=120)


class RecordDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_id:    UUID
    observation_id: UUID


@router.post("/resurface/decision", response_model=RecordDecisionResponse,
             status_code=status.HTTP_201_CREATED)
async def resurface_record_decision(
    body:      RecordDecisionRequest,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> RecordDecisionResponse:
    """Record a decision the agent made — structured fields the
    PatternDetector can mine for cross-agent decision-style learning.

    Sugar over POST /resurface/observation with a stable context
    convention. Returns decision_id (== observation_id) so the caller
    can later attach user reaction (accept/reject/modify) via a
    follow-up record_observation referring back."""
    state = _get_state(request)
    try:
        result = await state.resurface.record_decision(
            tenant_id=tenant_id,
            agent_id=body.agent_id,
            decision_kind=body.decision_kind,
            options=body.options,
            chosen=body.chosen,
            why=body.why,
            skill_id=body.skill_id,
            scope=body.scope,
        )
        return RecordDecisionResponse(
            decision_id=UUID(result["decision_id"]),
            observation_id=UUID(result["observation_id"]),
        )
    except Exception as e:
        log.error("resurface.record_decision.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.record_decision failed: {type(e).__name__}",
        ) from e


@router.get("/resurface/patterns", response_model=PatternsResult)
async def resurface_patterns(
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
    kinds:     Annotated[
        list[str] | None,
        Query(description="Filter by pattern kinds (any of: trending, fading, "
                          "forgotten, recurring, attention). Omit for all kinds."),
    ] = None,
    limit:     Annotated[int, Query(ge=1, le=200)] = 50,
    recompute_if_empty: Annotated[bool, Query()] = True,
) -> PatternsResult:
    """Dormant Brain — pre-scored pattern signals.

    Reads from resurface_patterns_cache. Each kind's signals have a TTL;
    expired rows are filtered out. If the cache is fully empty for the
    requested kinds AND recompute_if_empty=True, a synchronous
    PatternScorer.score_all() runs before serving (cold-start path);
    `cold_start_compute=true` in the response so the caller knows.

    Subsequent requests hit the warm cache. Phase 5a.3 (dreaming worker)
    takes ownership of automated background refresh — until then,
    cold-start-on-empty is the fallback.

    Tenant-isolated. Subject labels resolved server-side for convenience.
    """
    state = _get_state(request)
    try:
        return await state.resurface.get_patterns(
            tenant_id=tenant_id, kinds=kinds, limit=limit,
            recompute_if_empty=recompute_if_empty,
        )
    except Exception as e:
        log.error("resurface.patterns.failed",
                  tenant_id=str(tenant_id),
                  error=str(e), error_class=type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resurface.patterns failed: {type(e).__name__}",
        ) from e
