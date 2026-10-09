"""ResurfaceEngine — orchestrates resurfacing endpoint queries.

Architecturally constrained: reads ONLY from the v_* views layer added
in Migration 022. Never SELECTs from base tables. This is the
backend-stability contract that lets memory's internal schema evolve
without breaking resurfacing/agent code (Pillar 4).

Phase 5a.1 implements the Thinking Brain context endpoint. Future
phases plug in:
  5a.2 — Dormant Brain: pattern scorer + /resurface/patterns endpoint
  5a.3 — Dreaming worker (idle synthesis loop)
  5a.4 — Tool-Use Pattern engine + observations
  5a.5 — MCP server exposing all resurfacing tools
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog

from woxpas_resurfacing.observations import PatternDetector
from woxpas_resurfacing.opportunities import OpportunityDetector
from woxpas_resurfacing.score_maintainer import ScoreMaintainer
from woxpas_resurfacing.scorer import PatternScorer
from woxpas_resurfacing.types import (
    ActiveEntity,
    ActiveScope,
    ConflictBrief,
    IdeaBrief,
    ObservationRequest,
    OpenThreads,
    PatternSignal,
    PatternsResult,
    PredictedActionItem,
    PredictedActionListResponse,
    PredictedQuestionItem,
    PredictedQuestionListResponse,
    PreparedActionItem,
    PreparedActionListResponse,
    PreparedActionPostResponse,
    PreparedActionRequest,
    SessionSignals,
    TaskBrief,
    ThinkingBrainContext,
    TimelineEntry,
    ToolSuggestion,
    ToolSuggestions,
    UserPreferences,
)

log = structlog.get_logger(__name__)


# Tunables for Phase 5a.1
_ACTIVE_SCOPES_LIMIT       = 10
_ACTIVE_ENTITIES_LIMIT     = 20
_RECENT_TIMELINE_LIMIT     = 25
_TASKS_DUE_SOON_DAYS       = 7
_TASKS_DUE_SOON_LIMIT      = 10
_TASKS_OVERDUE_LIMIT       = 10
_IDEAS_DEVELOPING_LIMIT    = 10
_CONFLICTS_OPEN_LIMIT      = 10
_RECENT_DOCS_LIMIT         = 10


def _classify_time_of_day(dt: datetime) -> str:
    """Map an hour into a fuzzy time-of-day label.

    Boundaries are conservative — agents that need precise hours should
    consume `now` directly. The label is for the LLM's frame-setting
    ("you're talking to me at night, keep it brief")."""
    h = dt.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "night"


class ResurfaceEngine:
    """Read-only orchestrator over memory's view layer. Construct once;
    share across requests.

    Tenant isolation: every method takes tenant_id explicitly and the
    pool's tenant_session() applies RLS. The materialized
    v_mention_velocity has its own caveat (callers must filter by
    tenant_id explicitly per the migration comment) — handled in the
    relevant query below."""

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool
        self._scorer               = PatternScorer(pool=pool)
        self._score_maintainer     = ScoreMaintainer(pool=pool)
        self._pattern_detector     = PatternDetector(pool=pool)
        self._opportunity_detector = OpportunityDetector(pool=pool)

    async def thinking_context(
        self,
        *,
        tenant_id: UUID,
        max_entities: int = _ACTIVE_ENTITIES_LIMIT,
        max_timeline: int = _RECENT_TIMELINE_LIMIT,
        max_scopes:   int = _ACTIVE_SCOPES_LIMIT,
    ) -> ThinkingBrainContext:
        """Build the thinking-brain snapshot for this tenant.

        All queries run within a single tenant_session so RLS applies
        uniformly. Each sub-query is independent so failures in one
        subsystem (e.g., timeline outage) don't poison the whole response —
        we degrade individual fields to empty rather than failing the
        endpoint."""
        t_start = time.monotonic()
        metrics: dict[str, float] = {}

        # ─── Time signals ──────────────────────────────────────────────
        # Use tenant timezone if configured; fall back to UTC.
        tenant_tz_name = await self._fetch_tenant_timezone(tenant_id)
        try:
            tz = ZoneInfo(tenant_tz_name)
        except Exception:
            log.warning("resurfacing.invalid_timezone",
                        tenant_id=str(tenant_id), tz=tenant_tz_name)
            tz = ZoneInfo("UTC")
            tenant_tz_name = "UTC"

        now_utc = datetime.now(UTC)
        now_local = now_utc.astimezone(tz)

        # ─── Sub-queries ───────────────────────────────────────────────
        # Run sequentially for now; could be gather()'d if profiling shows
        # latency. Each one returns its slice of the snapshot.

        t0 = time.monotonic()
        active_scopes, scopes_truncated = await self._fetch_active_scopes(
            tenant_id, limit=max_scopes,
        )
        metrics["active_scopes_ms"] = round((time.monotonic() - t0) * 1000, 1)

        t0 = time.monotonic()
        active_entities, entities_truncated = await self._fetch_active_entities(
            tenant_id, limit=max_entities,
        )
        metrics["active_entities_ms"] = round((time.monotonic() - t0) * 1000, 1)

        t0 = time.monotonic()
        recent_timeline = await self._fetch_recent_timeline(
            tenant_id, limit=max_timeline,
        )
        metrics["timeline_ms"] = round((time.monotonic() - t0) * 1000, 1)

        t0 = time.monotonic()
        open_threads = await self._fetch_open_threads(tenant_id)
        metrics["open_threads_ms"] = round((time.monotonic() - t0) * 1000, 1)

        t0 = time.monotonic()
        session_signals = await self._fetch_session_signals(tenant_id)
        metrics["session_signals_ms"] = round((time.monotonic() - t0) * 1000, 1)

        metrics["total_ms"] = round((time.monotonic() - t_start) * 1000, 1)

        return ThinkingBrainContext(
            now=now_local,
            day_of_week=now_local.strftime("%A"),
            time_of_day=_classify_time_of_day(now_local),
            timezone=tenant_tz_name,
            location=None,                          # external signal not wired yet
            active_scopes=active_scopes,
            active_entities=active_entities,
            recent_timeline=recent_timeline,
            open_threads=open_threads,
            session_signals=session_signals,
            entities_truncated=entities_truncated,
            scopes_truncated=scopes_truncated,
            metrics=metrics,
        )

    async def refresh_scores(self, *, tenant_id: UUID) -> dict[str, Any]:
        """Run the ScoreMaintainer for one tenant. Exposed for manual
        triggers; Phase 5a.3's dreaming worker takes ownership of the
        automatic schedule."""
        return await self._score_maintainer.update_all_for_tenant(
            tenant_id=tenant_id,
        )

    # ─── private query methods (all read v_* views) ───────────────────────

    async def _fetch_tenant_timezone(self, tenant_id: UUID) -> str:
        """Tenants table has a `timezone` column. Default to UTC if NULL
        or row missing."""
        try:
            async with self._pool.tenant_session(tenant_id) as conn:
                tz = await conn.fetchval(
                    "SELECT timezone FROM tenants WHERE tenant_id = $1",
                    tenant_id,
                )
            return tz or "UTC"
        except Exception as e:
            log.warning("resurfacing.tenant_tz_fetch_failed",
                        tenant_id=str(tenant_id), error=str(e))
            return "UTC"

    async def _fetch_active_scopes(
        self, tenant_id: UUID, *, limit: int,
    ) -> tuple[list[ActiveScope], bool]:
        """Returns (scopes, truncated) — truncated=True iff there were
        more candidates than the limit (caller knows there's more to
        explore)."""
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT scope_id, slug, name, description,
                       doc_count_24h, doc_count_7d, last_touched_at
                  FROM v_scope_activity
                 WHERE last_touched_at > now() - interval '7 days'
                 ORDER BY last_touched_at DESC NULLS LAST
                 LIMIT $1 + 1                         -- fetch limit+1 to detect truncation
                """,
                limit,
            )
        truncated = len(rows) > limit
        rows = rows[:limit]
        return [
            ActiveScope(
                scope_id=r["scope_id"], slug=r["slug"], name=r["name"],
                description=r["description"],
                doc_count_24h=r["doc_count_24h"] or 0,
                doc_count_7d=r["doc_count_7d"] or 0,
                last_touched_at=r["last_touched_at"],
            )
            for r in rows
        ], truncated

    async def _fetch_active_entities(
        self, tenant_id: UUID, *, limit: int,
    ) -> tuple[list[ActiveEntity], bool]:
        # Phase 5a.2.1: ORDER BY surface_score DESC. Indexed via
        # entities_surface_score_idx (migration 025). Sub-millisecond at
        # any corpus size once ScoreMaintainer has populated scores.
        # Falls back to mention_count when surface_score is unset/0
        # (cold tenant before maintainer runs).
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    e.entity_id, e.name, e.type,
                    e.mention_count, e.last_mentioned_at,
                    e.access_count,  e.last_accessed_at,
                    e.decay_score, e.surface_score,
                    COALESCE(mv.mentions_24h, 0)            AS mentions_24h,
                    COALESCE(mv.mentions_7d, 0)             AS mentions_7d,
                    mv.slope_24h_vs_baseline                AS slope
                  FROM v_entities e
                  LEFT JOIN v_mention_velocity mv
                         ON mv.tenant_id = e.tenant_id
                        AND mv.entity_id = e.entity_id
                 WHERE e.last_mentioned_at > now() - interval '90 days'
                 ORDER BY e.surface_score DESC NULLS LAST,
                          e.mention_count DESC,
                          e.last_mentioned_at DESC NULLS LAST
                 LIMIT $1 + 1
                """,
                limit,
            )
        truncated = len(rows) > limit
        rows = rows[:limit]
        return [
            ActiveEntity(
                entity_id=r["entity_id"], name=r["name"], type=r["type"],
                mention_count=r["mention_count"] or 0,
                mentions_24h=r["mentions_24h"] or 0,
                mentions_7d=r["mentions_7d"] or 0,
                last_mentioned_at=r["last_mentioned_at"],
                access_count=r["access_count"] or 0,
                last_accessed_at=r["last_accessed_at"],
                decay_score=float(r["decay_score"] or 1.0),
                surface_score=float(r["surface_score"] or 0.0),
                slope_24h_vs_baseline=float(r["slope"]) if r["slope"] is not None else None,
            )
            for r in rows
        ], truncated

    async def _fetch_recent_timeline(
        self, tenant_id: UUID, *, limit: int,
    ) -> list[TimelineEntry]:
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, kind, occurred_at, actor,
                       doc_id, chunk_id, entity_id, idea_id, task_id,
                       scope_id, conflict_id, payload
                  FROM v_timeline
                 ORDER BY occurred_at DESC
                 LIMIT $1
                """,
                limit,
            )
        return [
            TimelineEntry(
                event_id=r["event_id"],
                kind=r["kind"],
                occurred_at=r["occurred_at"],
                actor=r["actor"],
                doc_id=r["doc_id"],
                entity_id=r["entity_id"],
                idea_id=r["idea_id"],
                task_id=r["task_id"],
                scope_id=r["scope_id"],
                conflict_id=r["conflict_id"],
                payload=dict(r["payload"]) if r["payload"] else {},
            )
            for r in rows
        ]

    async def _fetch_open_threads(self, tenant_id: UUID) -> OpenThreads:
        async with self._pool.tenant_session(tenant_id) as conn:
            tasks_due_soon = await conn.fetch(
                """
                SELECT task_id, title, kind, status, due_at, is_overdue, priority
                  FROM v_tasks
                 WHERE due_at BETWEEN now() AND now() + make_interval(days => $1)
                   AND status NOT IN ('done', 'cancelled')
                 ORDER BY due_at ASC NULLS LAST
                 LIMIT $2
                """,
                _TASKS_DUE_SOON_DAYS,
                _TASKS_DUE_SOON_LIMIT,
            )

            tasks_overdue = await conn.fetch(
                """
                SELECT task_id, title, kind, status, due_at, is_overdue, priority
                  FROM v_tasks
                 WHERE is_overdue
                 ORDER BY due_at ASC NULLS LAST
                 LIMIT $1
                """,
                _TASKS_OVERDUE_LIMIT,
            )

            ideas_developing = await conn.fetch(
                """
                SELECT idea_id, title, summary, status, category, last_revisited_at
                  FROM v_ideas
                 WHERE status IN ('developing', 'active')
                 ORDER BY last_revisited_at DESC NULLS LAST,
                          updated_at DESC
                 LIMIT $1
                """,
                _IDEAS_DEVELOPING_LIMIT,
            )

            conflicts = await conn.fetch(
                """
                SELECT conflict_id, kind, severity, notes, detected_at,
                       subject_entity_ids
                  FROM v_conflicts_open
                 ORDER BY severity DESC,         -- 'critical' / 'high' / 'medium' / 'low'
                          detected_at DESC
                 LIMIT $1
                """,
                _CONFLICTS_OPEN_LIMIT,
            )

        return OpenThreads(
            tasks_due_soon=[_task_brief_from_row(r) for r in tasks_due_soon],
            tasks_overdue=[_task_brief_from_row(r) for r in tasks_overdue],
            ideas_developing=[
                IdeaBrief(
                    idea_id=r["idea_id"], title=r["title"],
                    summary=r["summary"], status=r["status"],
                    category=r["category"],
                    last_revisited_at=r["last_revisited_at"],
                )
                for r in ideas_developing
            ],
            conflicts_open=[
                ConflictBrief(
                    conflict_id=r["conflict_id"], kind=r["kind"],
                    severity=r["severity"], notes=r["notes"],
                    detected_at=r["detected_at"],
                    subject_entity_ids=list(r["subject_entity_ids"] or []),
                )
                for r in conflicts
            ],
        )

    async def _fetch_session_signals(self, tenant_id: UUID) -> SessionSignals:
        # last_n_queries + recent_tools_called populate from
        # resurface_observations once Phase 5a.4 ships. v0.1 of this
        # endpoint returns empty arrays for those.
        async with self._pool.tenant_session(tenant_id) as conn:
            recent_docs = await conn.fetch(
                """
                SELECT doc_id
                  FROM v_documents
                 WHERE last_accessed_at > now() - interval '24 hours'
                 ORDER BY last_accessed_at DESC NULLS LAST
                 LIMIT $1
                """,
                _RECENT_DOCS_LIMIT,
            )
        return SessionSignals(
            last_n_queries=[],                                # populated in 5a.4
            recent_docs_accessed=[r["doc_id"] for r in recent_docs],
            recent_tools_called=[],                           # populated in 5a.4
        )


    # ─── Phase 5a.2 — Dormant Brain ────────────────────────────────────────

    async def get_patterns(
        self,
        *,
        tenant_id: UUID,
        kinds:     list[str] | None = None,
        limit:     int = 50,
        recompute_if_empty: bool = True,
    ) -> PatternsResult:
        """Read from resurface_patterns_cache, optionally filtered by
        kinds. If the cache has nothing for any of the requested kinds
        (cold start), trigger PatternScorer.score_all() synchronously
        before serving — so the FIRST call returns useful data.

        Subsequent calls hit the cache. The 5a.3 dreaming worker takes
        ownership of automated refresh; for now (5a.2) the cache only
        repopulates on demand."""
        t_start = time.monotonic()
        valid_kinds = ("trending", "fading", "forgotten", "recurring", "attention")
        wanted = [k for k in (kinds or valid_kinds) if k in valid_kinds]
        if not wanted:
            wanted = list(valid_kinds)

        # Cold-start check: if cache is fully empty for ALL wanted kinds,
        # trigger synchronous compute. Otherwise serve what's there.
        cache_empty = False
        if recompute_if_empty:
            async with self._pool.tenant_session(tenant_id) as conn:
                row = await conn.fetchrow(
                    """
                    SELECT count(*) AS n FROM resurface_patterns_cache
                     WHERE kind = ANY ($1::resurface_pattern_kind[])
                    """,
                    wanted,
                )
            cache_empty = (row["n"] or 0) == 0
        if cache_empty:
            log.info("resurfacing.patterns.cold_start_compute",
                     tenant_id=str(tenant_id), kinds=wanted)
            await self._scorer.score_all(tenant_id=tenant_id)

        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT pattern_id, kind, subject_kind, subject_id,
                       score, reason, computed_at, ttl_at
                  FROM resurface_patterns_cache
                 WHERE kind = ANY ($1::resurface_pattern_kind[])
                   AND ttl_at > now()
                 ORDER BY kind, score DESC
                 LIMIT $2
                """,
                wanted, limit,
            )

            # Resolve subject names for the response (single batched query
            # per subject_kind for efficiency). Agent gets human-readable
            # labels without separate calls.
            entity_ids   = [r["subject_id"] for r in rows
                            if r["subject_kind"] == "entity"]
            idea_ids     = [r["subject_id"] for r in rows
                            if r["subject_kind"] == "idea"]
            task_ids     = [r["subject_id"] for r in rows
                            if r["subject_kind"] == "task"]
            conflict_ids = [r["subject_id"] for r in rows
                            if r["subject_kind"] == "conflict"]

            entity_names: dict[UUID, str] = {}
            if entity_ids:
                erows = await conn.fetch(
                    "SELECT entity_id, name FROM v_entities "
                    "WHERE entity_id = ANY ($1::uuid[])",
                    entity_ids,
                )
                entity_names = {r["entity_id"]: r["name"] for r in erows}

            idea_titles: dict[UUID, str] = {}
            if idea_ids:
                irows = await conn.fetch(
                    "SELECT idea_id, COALESCE(title, summary) AS label "
                    "FROM v_ideas WHERE idea_id = ANY ($1::uuid[])",
                    idea_ids,
                )
                idea_titles = {r["idea_id"]: r["label"] for r in irows}

            task_titles: dict[UUID, str] = {}
            if task_ids:
                trows = await conn.fetch(
                    "SELECT task_id, title FROM v_tasks "
                    "WHERE task_id = ANY ($1::uuid[])",
                    task_ids,
                )
                task_titles = {r["task_id"]: r["title"] for r in trows}

            conflict_labels: dict[UUID, str] = {}
            if conflict_ids:
                crows = await conn.fetch(
                    "SELECT conflict_id, kind, severity FROM v_conflicts_open "
                    "WHERE conflict_id = ANY ($1::uuid[])",
                    conflict_ids,
                )
                conflict_labels = {
                    r["conflict_id"]: f"{r['kind']} ({r['severity']})"
                    for r in crows
                }

        signals: list[PatternSignal] = []
        for r in rows:
            sk = r["subject_kind"]
            sid = r["subject_id"]
            label = (
                entity_names.get(sid)    if sk == "entity"
                else idea_titles.get(sid)    if sk == "idea"
                else task_titles.get(sid)    if sk == "task"
                else conflict_labels.get(sid) if sk == "conflict"
                else None
            )
            signals.append(PatternSignal(
                pattern_id=r["pattern_id"],
                kind=r["kind"],
                subject_kind=r["subject_kind"],
                subject_id=r["subject_id"],
                subject_label=label,
                score=float(r["score"]),
                reason=r["reason"],
                computed_at=r["computed_at"],
                ttl_at=r["ttl_at"],
            ))

        return PatternsResult(
            patterns=signals,
            kinds_requested=wanted,
            total_returned=len(signals),
            cold_start_compute=cache_empty,
            metrics={"total_ms": round((time.monotonic() - t_start) * 1000, 1)},
        )


    # ─── Phase 5a.4 — observations + preferences ──────────────────────────

    async def record_observation(
        self,
        *,
        tenant_id: UUID,
        body:      ObservationRequest,
    ) -> None:
        """Insert a single observation row. Called by any agent (an agent
        + 3rd-party MCP) on every interaction. Tenant-isolated; cheap
        write (~1ms). PatternDetector reads these later in batched
        passes — agents never wait for analysis."""
        async with self._pool.tenant_session(tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO resurface_observations (
                    tenant_id, agent_id, observed_at,
                    context, tool_called, skill_id, tool_input, outcome
                )
                VALUES ($1, $2, now(), $3, $4, $5, $6, $7)
                """,
                tenant_id, body.agent_id,
                body.context or {},
                body.tool_called,
                body.skill_id,
                body.tool_input,
                body.outcome or {},
            )

    async def get_preferences(
        self, *, tenant_id: UUID,
    ) -> UserPreferences:
        """Read the live preferences snapshot. Falls back to defaults
        when no row exists yet (cold tenant). The dreaming worker keeps
        this fresh; agents read here on every conversational turn."""
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT answer_length_preference, citation_density_preference,
                       persona_tone, tool_use_preferences,
                       surfacing_aggressiveness, updated_at
                  FROM resurface_user_preferences
                 WHERE tenant_id = $1
                """,
                tenant_id,
            )

            obs_count = await conn.fetchval(
                """
                SELECT count(*) FROM resurface_observations
                 WHERE observed_at > now() - interval '30 days'
                """
            )

        if row is None:
            return UserPreferences(observation_count_30d=int(obs_count or 0))

        tup = dict(row["tool_use_preferences"] or {})
        return UserPreferences(
            answer_length_preference    = row["answer_length_preference"] or "normal",
            citation_density_preference = row["citation_density_preference"] or "normal",
            surfacing_aggressiveness    = row["surfacing_aggressiveness"]    or "normal",
            persona_tone                = dict(row["persona_tone"] or {}),
            tool_use_preferences        = tup,
            most_active_hour            = tup.get("most_active_hour"),
            most_active_day_of_week     = tup.get("most_active_day_of_week"),
            top_tools_used              = list(tup.get("top_tools_used") or []),
            top_skills_used             = list(tup.get("top_skills_used") or []),
            tool_by_hour                = dict(tup.get("tool_by_hour")  or {}),
            skill_by_hour               = dict(tup.get("skill_by_hour") or {}),
            updated_at                  = row["updated_at"],
            observation_count_30d       = int(obs_count or 0),
        )

    async def get_tool_suggestions(
        self,
        *,
        tenant_id: UUID,
        context:   dict[str, Any] | None = None,
        limit:     int = 5,
    ) -> ToolSuggestions:
        """Frequency-ranked tool suggestions for the agent's current
        context. v0.1: simple counter over last 30 days of observations.
        v0.2: context-aware sequencing (A→B chains)."""
        t_start = time.monotonic()
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT tool_called,
                       count(*)         AS invocations,
                       MAX(observed_at) AS last_used
                  FROM resurface_observations
                 WHERE observed_at > now() - interval '30 days'
                 GROUP BY tool_called
                 ORDER BY count(*) DESC
                 LIMIT $1
                """,
                limit,
            )

        cold = len(rows) == 0
        suggestions = [
            ToolSuggestion(
                tool_name=r["tool_called"],
                score=float(r["invocations"]),
                reason=f"called {r['invocations']} times in last 30 days",
                last_used_at=r["last_used"],
                invocation_count=int(r["invocations"]),
            )
            for r in rows
        ]
        return ToolSuggestions(
            suggestions=suggestions,
            context_used=context or {},
            cold_start=cold,
            metrics={"total_ms": round((time.monotonic() - t_start) * 1000, 1)},
        )

    async def detect_patterns(self, *, tenant_id: UUID) -> dict[str, Any]:
        """Trigger PatternDetector for one tenant. Exposed for manual
        invocation; the dreaming worker calls this on schedule."""
        return await self._pattern_detector.update_preferences_for_tenant(
            tenant_id=tenant_id,
        )

    async def detect_opportunities(self, *, tenant_id: UUID) -> dict[str, Any]:
        """Trigger OpportunityDetector for one tenant (Phase 5a.3.1).
        Exposed for manual invocation + sync cold-start fallback when
        a list endpoint is hit on a tenant the dreaming worker hasn't
        processed yet."""
        return await self._opportunity_detector.detect_for_tenant(
            tenant_id=tenant_id,
        )

    # ─── Phase 5a.5+ — agent-facing writes ──────────────────────────────

    async def upsert_persona(
        self, *, tenant_id: UUID, persona_patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Deep-merge `persona_patch` into resurface_user_preferences.persona_tone.

        Tone profile is BEHAVIORAL data — observed style/cadence/register
        per channel + per relationship. Lives in resurfacing layer, NOT
        in the KG (relations are world facts; tone is observed pattern).

        Patch is deep-merged: top-level keys get replaced when present,
        nested dicts get merged recursively. Use NULL value to clear
        a key (e.g., {'email': None} drops the email persona).

        Typical usage:
          - On first connect to a channel: upsert_persona({
              'default': {'email': {'register': 'professional', ...}}
            })
          - Per-relationship learning: upsert_persona({
              'per_relationship': {'<sarah-uuid>': {'email': {...}}}
            })
        """
        async with self._pool.tenant_session(tenant_id) as conn:
            # Read current; deep-merge in Python; write back. One-shot
            # is acceptable because resurface_user_preferences has at
            # most one row per tenant and this isn't a hot path.
            row = await conn.fetchrow(
                "SELECT persona_tone::text AS p "
                " FROM resurface_user_preferences WHERE tenant_id = $1",
                tenant_id,
            )
            import json as _json
            current: dict[str, Any] = _json.loads(row["p"]) if row else {}

            merged = _deep_merge(current, persona_patch)

            await conn.execute(
                """
                INSERT INTO resurface_user_preferences (tenant_id, persona_tone, updated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (tenant_id) DO UPDATE SET
                    persona_tone = EXCLUDED.persona_tone,
                    updated_at   = now()
                """,
                tenant_id, merged,
            )
        log.info("resurface.upsert_persona.done",
                 tenant_id=str(tenant_id),
                 keys=list(persona_patch.keys()))
        return {"persona_tone": merged}

    async def record_decision(
        self, *,
        tenant_id:     UUID,
        agent_id:      str,
        decision_kind: str,
        options:       list[Any],
        chosen:        Any,
        why:           str | None = None,
        skill_id:      str | None = None,
        scope:         str | None = None,
    ) -> dict[str, Any]:
        """Record a decision the agent made, with structured fields the
        PatternDetector can later mine for X.2 (decision-style learning).

        Returns {decision_id, observation_id} — both equal. an agent stores
        the id and can attach user_reaction later via record_observation
        with a context hint pointing back.

        Implementation: thin wrapper over resurface_observations with a
        stable context convention so future detectors can aggregate
        without schema changes."""
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO resurface_observations
                  (tenant_id, agent_id, tool_called, skill_id,
                   tool_input, outcome, context)
                VALUES ($1, $2, 'agent.decide', $3,
                        $4::jsonb, $5::jsonb, $6::jsonb)
                RETURNING observation_id
                """,
                tenant_id, agent_id, skill_id,
                {"decision_kind": decision_kind},
                {"success": True, "user_engaged": None},
                {
                    "decision": {
                        "kind":    decision_kind,
                        "options": options,
                        "chosen":  chosen,
                        "why":     why,
                    },
                    "scope": scope,
                },
            )
        decision_id = row["observation_id"]
        log.info("resurface.record_decision.done",
                 tenant_id=str(tenant_id),
                 decision_kind=decision_kind, decision_id=str(decision_id))
        return {
            "decision_id":     str(decision_id),
            "observation_id":  str(decision_id),
        }

    # ─── Phase 5a.3.1 — Predicted/Prepared action read paths ─────────────

    async def list_predicted_actions(
        self, *, tenant_id: UUID,
        action_kinds: list[str] | None = None,
        subject_kind: str | None = None,
        limit: int = 50,
    ) -> PredictedActionListResponse:
        """Memory-internal opportunities surfaced by the dreaming worker.
        Filtered by action_kind / subject_kind. TTL-respecting — expired
        artifacts are excluded server-side. Falls back to sync compute
        if the cache is fully empty (cold tenant)."""
        t_start = time.monotonic()

        async with self._pool.tenant_session(tenant_id) as conn:
            existing = await conn.fetchval(
                """
                SELECT count(*) FROM resurface_artifacts
                 WHERE kind = 'predicted_action'
                   AND (expires_at IS NULL OR expires_at > now())
                """
            )

        cold_start = False
        if (existing or 0) == 0:
            await self.detect_opportunities(tenant_id=tenant_id)
            cold_start = True

        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT artifact_id, content, status, requires_approval,
                       generated_at, expires_at
                  FROM resurface_artifacts
                 WHERE kind = 'predicted_action'
                   AND (expires_at IS NULL OR expires_at > now())
                   AND ($1::text[] IS NULL
                        OR (content->>'action_kind') = ANY($1::text[]))
                   AND ($2::text IS NULL
                        OR (content->>'subject_kind') = $2::text)
                 ORDER BY generated_at DESC
                 LIMIT $3
                """,
                action_kinds, subject_kind, limit,
            )

        items: list[PredictedActionItem] = []
        for r in rows:
            c = r["content"] or {}
            try:
                subject_id = UUID(c.get("subject_id"))  # stored as text
            except (TypeError, ValueError):
                continue
            items.append(PredictedActionItem(
                artifact_id       = r["artifact_id"],
                action_kind       = c.get("action_kind", "unknown"),
                subject_kind      = c.get("subject_kind", "unknown"),
                subject_id        = subject_id,
                subject_label     = c.get("subject_label"),
                urgency           = c.get("urgency", "medium"),
                context           = dict(c.get("context") or {}),
                suggested_actions = list(c.get("suggested_actions") or []),
                status            = r["status"],
                requires_approval = bool(r["requires_approval"]),
                generated_at      = r["generated_at"],
                expires_at        = r["expires_at"],
            ))

        return PredictedActionListResponse(
            items=items, total=len(items),
            metrics={
                "ms": round((time.monotonic() - t_start) * 1000, 1),
                "cold_start_compute": cold_start,
            },
        )

    async def list_predicted_questions(
        self, *, tenant_id: UUID,
        subject_id: UUID | None = None,
        limit: int = 25,
    ) -> PredictedQuestionListResponse:
        """Pre-cached synthesis answers from QuestionPreCacher (5a.3.1).
        TTL-respecting. Empty when the dreaming worker hasn't generated
        any yet OR when cloud is disallowed and no cached entries exist."""
        t_start = time.monotonic()

        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT artifact_id, content, cloud_egress_used,
                       generated_at, expires_at
                  FROM resurface_artifacts
                 WHERE kind = 'predicted_question'
                   AND (expires_at IS NULL OR expires_at > now())
                   AND ($1::uuid IS NULL
                        OR (content->>'subject_id')::uuid = $1::uuid)
                 ORDER BY generated_at DESC
                 LIMIT $2
                """,
                subject_id, limit,
            )

        items: list[PredictedQuestionItem] = []
        for r in rows:
            c = r["content"] or {}
            sid_text = c.get("subject_id")
            try:
                sid = UUID(sid_text) if sid_text else None
            except (TypeError, ValueError):
                sid = None
            items.append(PredictedQuestionItem(
                artifact_id    = r["artifact_id"],
                subject_kind   = c.get("subject_kind", "unknown"),
                subject_id     = sid,
                subject_label  = c.get("subject_label"),
                question_text  = c.get("question_text", ""),
                answer_text    = c.get("answer_text", ""),
                citation_count = int(c.get("citation_count") or 0),
                cloud_egress   = bool(r["cloud_egress_used"]),
                generated_at   = r["generated_at"],
                expires_at     = r["expires_at"],
            ))

        return PredictedQuestionListResponse(
            items=items, total=len(items),
            metrics={"ms": round((time.monotonic() - t_start) * 1000, 1)},
        )

    # ─── Phase 5a.3.1 — Prepared action bridge (write + state machine) ──

    async def post_prepared_action(
        self, *, tenant_id: UUID, body: PreparedActionRequest,
    ) -> PreparedActionPostResponse:
        """Create a prepared_action artifact. Agent-supplied — memory
        does not validate the draft itself."""
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO resurface_artifacts (
                    tenant_id, kind, content, related_entity_ids,
                    expires_at, status, requires_approval, agent_id
                )
                VALUES (
                    $1::uuid,
                    'prepared_action'::resurface_artifact_kind,
                    jsonb_build_object(
                        'action_kind',   $2::text,
                        'subject_label', $3::text,
                        'subject_id',    $4::text,
                        'draft_content', $5::jsonb
                    ),
                    $6::uuid[],
                    now() + make_interval(hours => $7),
                    'drafted'::resurface_artifact_status,
                    $8::boolean,
                    $9::text
                )
                RETURNING artifact_id, status, expires_at
                """,
                tenant_id,
                body.action_kind,
                body.subject_label,
                str(body.subject_id) if body.subject_id else None,
                body.draft_content,
                body.related_entity_ids,
                body.expires_in_hours,
                body.requires_approval,
                body.agent_id,
            )
        return PreparedActionPostResponse(
            artifact_id = row["artifact_id"],
            status      = row["status"],
            expires_at  = row["expires_at"],
        )

    async def list_prepared_actions(
        self, *, tenant_id: UUID,
        statuses:    list[str] | None = None,
        agent_id:    str | None = None,
        action_kind: str | None = None,
        expiring_within_hours: int | None = None,
        limit: int = 50,
    ) -> PreparedActionListResponse:
        """List prepared_action artifacts with filters. Default lists
        only drafted+approved (i.e., still actionable). Pass
        statuses=['drafted','approved','executed','rejected','expired']
        for full history."""
        t_start = time.monotonic()
        if statuses is None:
            statuses = ["drafted", "approved"]

        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT artifact_id, content, related_entity_ids, agent_id,
                       status, requires_approval, generated_at, expires_at,
                       approved_at, executed_at, rejected_at, rejected_reason
                  FROM resurface_artifacts
                 WHERE kind = 'prepared_action'
                   AND status::text = ANY($1::text[])
                   AND ($2::text IS NULL OR agent_id = $2::text)
                   AND ($3::text IS NULL
                        OR (content->>'action_kind') = $3::text)
                   AND ($4::int IS NULL
                        OR (expires_at IS NOT NULL
                            AND expires_at < now() + make_interval(hours => $4)))
                 ORDER BY generated_at DESC
                 LIMIT $5
                """,
                statuses, agent_id, action_kind,
                expiring_within_hours, limit,
            )

        items: list[PreparedActionItem] = []
        for r in rows:
            c = r["content"] or {}
            sid_text = c.get("subject_id")
            try:
                sid = UUID(sid_text) if sid_text else None
            except (TypeError, ValueError):
                sid = None
            items.append(PreparedActionItem(
                artifact_id        = r["artifact_id"],
                agent_id           = r["agent_id"],
                action_kind        = c.get("action_kind", "unknown"),
                subject_label      = c.get("subject_label"),
                subject_id         = sid,
                draft_content      = dict(c.get("draft_content") or {}),
                status             = r["status"],
                requires_approval  = bool(r["requires_approval"]),
                generated_at       = r["generated_at"],
                expires_at         = r["expires_at"],
                approved_at        = r["approved_at"],
                executed_at        = r["executed_at"],
                rejected_at        = r["rejected_at"],
                rejected_reason    = r["rejected_reason"],
                related_entity_ids = list(r["related_entity_ids"] or []),
            ))

        return PreparedActionListResponse(
            items=items, total=len(items),
            metrics={"ms": round((time.monotonic() - t_start) * 1000, 1)},
        )

    async def approve_prepared_action(
        self, *, tenant_id: UUID, artifact_id: UUID,
    ) -> PreparedActionItem:
        """drafted → approved. Idempotent on already-approved (returns
        current state). Errors if status is executed/rejected/expired."""
        return await self._transition_prepared(
            tenant_id=tenant_id, artifact_id=artifact_id,
            target_status="approved", from_statuses=["drafted", "approved"],
            timestamp_col="approved_at",
        )

    async def reject_prepared_action(
        self, *, tenant_id: UUID, artifact_id: UUID,
        reason: str | None = None,
    ) -> PreparedActionItem:
        """drafted | approved → rejected. Captures reason if provided."""
        return await self._transition_prepared(
            tenant_id=tenant_id, artifact_id=artifact_id,
            target_status="rejected", from_statuses=["drafted", "approved"],
            timestamp_col="rejected_at", rejected_reason=reason,
        )

    async def mark_prepared_executed(
        self, *, tenant_id: UUID, artifact_id: UUID,
    ) -> PreparedActionItem:
        """approved → executed. Agent reports back after running the
        action via its own connector. Memory persists the receipt for
        audit + dedupe."""
        return await self._transition_prepared(
            tenant_id=tenant_id, artifact_id=artifact_id,
            target_status="executed", from_statuses=["approved"],
            timestamp_col="executed_at",
        )

    async def _transition_prepared(
        self, *, tenant_id: UUID, artifact_id: UUID,
        target_status:   str,
        from_statuses:   list[str],
        timestamp_col:   str,
        rejected_reason: str | None = None,
    ) -> PreparedActionItem:
        """Generic state-machine transition. Raises ValueError if the
        artifact doesn't exist or is in an incompatible state."""
        valid_cols = {"approved_at", "executed_at", "rejected_at"}
        if timestamp_col not in valid_cols:
            raise ValueError(f"invalid timestamp_col: {timestamp_col}")

        async with self._pool.tenant_session(tenant_id) as conn:
            # Phase 5a.3.2 fix D: refuse transitions on TTL-expired
            # artifacts. The contract says "drafted → expired by TTL";
            # accepting a state mutation past expiry violates it.
            row = await conn.fetchrow(
                f"""
                UPDATE resurface_artifacts
                   SET status            = $3::resurface_artifact_status,
                       {timestamp_col}   = now(),
                       rejected_reason   = COALESCE($4, rejected_reason)
                 WHERE artifact_id = $1
                   AND kind = 'prepared_action'
                   AND status::text = ANY($2::text[])
                   AND (expires_at IS NULL OR expires_at > now())
                RETURNING
                   artifact_id, content, related_entity_ids, agent_id,
                   status, requires_approval, generated_at, expires_at,
                   approved_at, executed_at, rejected_at, rejected_reason
                """,
                artifact_id, from_statuses, target_status, rejected_reason,
            )

        if row is None:
            raise ValueError(
                f"prepared_action {artifact_id} not found, not in "
                f"{from_statuses}, or past TTL"
            )

        c = row["content"] or {}
        sid_text = c.get("subject_id")
        try:
            sid = UUID(sid_text) if sid_text else None
        except (TypeError, ValueError):
            sid = None
        return PreparedActionItem(
            artifact_id        = row["artifact_id"],
            agent_id           = row["agent_id"],
            action_kind        = c.get("action_kind", "unknown"),
            subject_label      = c.get("subject_label"),
            subject_id         = sid,
            draft_content      = dict(c.get("draft_content") or {}),
            status             = row["status"],
            requires_approval  = bool(row["requires_approval"]),
            generated_at       = row["generated_at"],
            expires_at         = row["expires_at"],
            approved_at        = row["approved_at"],
            executed_at        = row["executed_at"],
            rejected_at        = row["rejected_at"],
            rejected_reason    = row["rejected_reason"],
            related_entity_ids = list(row["related_entity_ids"] or []),
        )


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge patch into base. Nested dicts merge recursively;
    other types replace. NULL value drops the key (caller's signal to
    clear). Returns a new dict; doesn't mutate base."""
    out = dict(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _task_brief_from_row(r: Any) -> TaskBrief:
    return TaskBrief(
        task_id=r["task_id"], title=r["title"], kind=r["kind"],
        status=r["status"], due_at=r["due_at"],
        is_overdue=bool(r["is_overdue"]), priority=r["priority"],
    )
