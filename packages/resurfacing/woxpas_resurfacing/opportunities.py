"""OpportunityDetector — Phase 5a.3.1 memory-internal action opportunities.

Walks the user's CAPTURED knowledge graph (v_tasks / v_conflicts_open /
v_ideas / v_entities / v_documents) and surfaces resurface_artifacts of
kind='predicted_action'. These are agent-agnostic suggestions: any agent
that calls GET /resurface/predicted-actions sees them.

Action kinds (taxonomy stored in artifact.content['action_kind']):

  task_overdue     — task with is_overdue=true and status not done/cancelled
  conflict_open    — open conflict that has not been resolved
  idea_dormant     — active idea not revisited in 30+ days
  entity_stale     — entity with mention_count > 5 and no access in 14+ days
  doc_unscoped     — recent document (<7d) not assigned to any scope

  recurring_missed — DEFERRED to v0.2. Requires PatternScorer's recurring
                     output PLUS a "did expected next mention happen?"
                     check. The recurring detection already runs; surfacing
                     a *missed* occurrence needs cadence-aware diffing
                     that's beyond what one SQL aggregation can do cleanly.
                     Add when telemetry shows users want it.

Idempotency: each opportunity row is keyed on a deterministic
`subject_signature` ('<action_kind>:<subject_kind>:<subject_id>') and
UPSERTed via `ux_predicted_action_signature`. Reruns refresh content +
extend TTL rather than spam duplicates. When an opportunity resolves
(task marked done, conflict closed) the next dreaming pass simply
skips it; the existing artifact ages out via TTL.

Memory has zero domain-specific logic here — there is no "email" or
"calendar" code in this module. an agent (or any agent) layers external-
service opportunities on top by traversing services via their MCPs and
posting prepared_action artifacts back through the bridge endpoints.

Cheap to run — five SQL aggregations over views that are already
indexed. Typical pass for an active tenant: 5-30ms.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────

_OPPORTUNITY_TTL = timedelta(days=7)
"""Predicted_action artifacts auto-expire after a week. Reruns refresh
TTL on still-valid opportunities. Reaper sweeps WHERE expires_at < now()
once daily (deferred to v0.2 — see project_v02_followups.md)."""

_IDEA_DORMANT_DAYS    = 30
_ENTITY_STALE_DAYS    = 14
_ENTITY_MIN_MENTIONS  = 5     # avoid noise from one-off entities
_DOC_UNSCOPED_DAYS    = 7     # only recent unscoped docs are interesting
_ROW_CAP_PER_KIND     = 100   # avoid runaway artifact creation

_ACTION_KINDS = (
    "task_overdue",
    "conflict_open",
    "idea_dormant",
    "entity_stale",
    "doc_unscoped",
)


# ─── Detector ──────────────────────────────────────────────────────────────

class OpportunityDetector:
    """Surfaces memory-internal action opportunities. Tenant-isolated via
    tenant_session. Idempotent — running back-to-back produces the same
    set of artifacts with refreshed TTLs."""

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool

    async def detect_for_tenant(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Run all detectors for a tenant, write artifacts, return counts.

        Each detector first DELETEs stale artifacts whose underlying
        condition no longer holds (Phase 5a.3.2 fix) — this is what
        keeps "task_overdue: X" from lingering for the full TTL after
        the user marks X done. Then UPSERTs current opportunities.
        """
        t_start = time.monotonic()
        counts: dict[str, Any] = {}
        stale_removed: dict[str, int] = {}

        for kind in _ACTION_KINDS:
            try:
                fn = getattr(self, f"_detect_{kind}")
                stale, fresh = await fn(
                    tenant_id=tenant_id, dreaming_run_id=dreaming_run_id,
                )
                counts[kind] = fresh
                if stale:
                    stale_removed[kind] = stale
            except Exception as e:
                log.error("opportunities.detect_failed",
                          tenant_id=str(tenant_id), action_kind=kind,
                          error_class=type(e).__name__,
                          error=str(e)[:200])
                counts[kind] = -1

        counts["ms"] = round((time.monotonic() - t_start) * 1000, 1)
        if stale_removed:
            counts["_stale_removed"] = stale_removed
        log.info("opportunities.done",
                 tenant_id=str(tenant_id), counts=counts)
        return counts

    # ─── Per-kind detectors ────────────────────────────────────────────────

    async def _detect_task_overdue(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None,
    ) -> tuple[int, int]:
        """Overdue tasks the user captured. Suggested actions: reschedule,
        mark done, delegate. Reversibility=high — none of these execute
        external side-effects, just memory state changes."""
        async with self._pool.tenant_session(tenant_id) as conn:
            # Phase 5a.3.2 fix A: drop artifacts whose condition resolved
            # (task done/cancelled) or whose subject was deleted.
            stale = await conn.fetchval(
                """
                WITH live AS (
                  SELECT 'task_overdue:task:' || task_id::text AS sig
                    FROM v_tasks
                   WHERE is_overdue = true
                     AND status NOT IN ('done', 'cancelled')
                ),
                del AS (
                  DELETE FROM resurface_artifacts
                   WHERE kind = 'predicted_action'
                     AND subject_signature LIKE 'task_overdue:%'
                     AND NOT EXISTS (
                       SELECT 1 FROM live WHERE live.sig = subject_signature
                     )
                  RETURNING 1
                )
                SELECT count(*) FROM del
                """
            ) or 0

            fresh = await conn.fetchval(
                """
                WITH src AS (
                  SELECT
                    task_id,
                    title,
                    kind        AS task_kind,
                    status,
                    due_at,
                    priority,
                    owner_entity_id,
                    related_idea_id
                  FROM v_tasks
                  WHERE is_overdue = true
                    AND status NOT IN ('done', 'cancelled')
                  ORDER BY due_at ASC NULLS LAST
                  LIMIT $2
                )
                INSERT INTO resurface_artifacts (
                  tenant_id, kind, content, related_entity_ids,
                  expires_at, dreaming_run_id, status,
                  requires_approval, subject_signature
                )
                SELECT
                  $1::uuid,
                  'predicted_action'::resurface_artifact_kind,
                  jsonb_build_object(
                    'action_kind',     'task_overdue',
                    'subject_kind',    'task',
                    'subject_id',      task_id,
                    'subject_label',   title,
                    'context', jsonb_build_object(
                      'task_kind',  task_kind,
                      'status',     status,
                      'due_at',     due_at,
                      'priority',   priority,
                      'days_overdue',
                        CASE WHEN due_at IS NULL THEN NULL
                             ELSE EXTRACT(epoch FROM (now() - due_at))/86400.0
                        END
                    ),
                    'suggested_actions', jsonb_build_array(
                      jsonb_build_object('name','reschedule',  'reversibility','high','requires_approval', false),
                      jsonb_build_object('name','mark_done',   'reversibility','medium','requires_approval', true),
                      jsonb_build_object('name','delegate',    'reversibility','high','requires_approval', true),
                      jsonb_build_object('name','cancel',      'reversibility','medium','requires_approval', true)
                    )
                  ),
                  COALESCE(ARRAY[owner_entity_id]::uuid[], '{}'),
                  now() + ($3::interval),
                  $4::uuid,
                  'drafted'::resurface_artifact_status,
                  false,
                  'task_overdue:task:' || task_id::text
                FROM src
                ON CONFLICT (tenant_id, subject_signature)
                  WHERE kind = 'predicted_action' AND subject_signature IS NOT NULL
                DO UPDATE SET
                  content         = EXCLUDED.content,
                  expires_at      = EXCLUDED.expires_at,
                  dreaming_run_id = EXCLUDED.dreaming_run_id,
                  generated_at    = now()
                RETURNING (SELECT count(*) FROM src)
                """,
                tenant_id, _ROW_CAP_PER_KIND,
                _OPPORTUNITY_TTL, dreaming_run_id,
            ) or 0
            return int(stale), int(fresh)

    async def _detect_conflict_open(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None,
    ) -> tuple[int, int]:
        """Open conflicts in the knowledge graph. Suggested action:
        present-for-resolution (pick / coexist / progression / dismiss)."""
        async with self._pool.tenant_session(tenant_id) as conn:
            stale = await conn.fetchval(
                """
                WITH live AS (
                  SELECT 'conflict_open:conflict:' || conflict_id::text AS sig
                    FROM v_conflicts_open
                ),
                del AS (
                  DELETE FROM resurface_artifacts
                   WHERE kind = 'predicted_action'
                     AND subject_signature LIKE 'conflict_open:%'
                     AND NOT EXISTS (
                       SELECT 1 FROM live WHERE live.sig = subject_signature
                     )
                  RETURNING 1
                )
                SELECT count(*) FROM del
                """
            ) or 0

            fresh = await conn.fetchval(
                """
                WITH src AS (
                  SELECT
                    conflict_id, kind AS conflict_kind, severity, predicate,
                    notes, detected_at, subject_entity_ids
                  FROM v_conflicts_open
                  ORDER BY
                    CASE severity
                      WHEN 'critical' THEN 0
                      WHEN 'high'     THEN 1
                      WHEN 'medium'   THEN 2
                      WHEN 'low'      THEN 3
                      ELSE 4
                    END,
                    detected_at ASC
                  LIMIT $2
                )
                INSERT INTO resurface_artifacts (
                  tenant_id, kind, content, related_entity_ids,
                  expires_at, dreaming_run_id, status,
                  requires_approval, subject_signature
                )
                SELECT
                  $1::uuid,
                  'predicted_action'::resurface_artifact_kind,
                  jsonb_build_object(
                    'action_kind',     'conflict_open',
                    'subject_kind',    'conflict',
                    'subject_id',      conflict_id,
                    'subject_label',   COALESCE(notes, predicate, conflict_kind),
                    'context', jsonb_build_object(
                      'conflict_kind', conflict_kind,
                      'severity',      severity,
                      'predicate',     predicate,
                      'detected_at',   detected_at
                    ),
                    'suggested_actions', jsonb_build_array(
                      jsonb_build_object('name','resolve_pick',         'reversibility','medium','requires_approval', true),
                      jsonb_build_object('name','resolve_coexistence',  'reversibility','medium','requires_approval', true),
                      jsonb_build_object('name','resolve_progression',  'reversibility','medium','requires_approval', true),
                      jsonb_build_object('name','dismiss',              'reversibility','high',  'requires_approval', true)
                    )
                  ),
                  COALESCE(subject_entity_ids, '{}'),
                  now() + ($3::interval),
                  $4::uuid,
                  'drafted'::resurface_artifact_status,
                  true,
                  'conflict_open:conflict:' || conflict_id::text
                FROM src
                ON CONFLICT (tenant_id, subject_signature)
                  WHERE kind = 'predicted_action' AND subject_signature IS NOT NULL
                DO UPDATE SET
                  content         = EXCLUDED.content,
                  expires_at      = EXCLUDED.expires_at,
                  dreaming_run_id = EXCLUDED.dreaming_run_id,
                  generated_at    = now()
                RETURNING (SELECT count(*) FROM src)
                """,
                tenant_id, _ROW_CAP_PER_KIND,
                _OPPORTUNITY_TTL, dreaming_run_id,
            ) or 0
            return int(stale), int(fresh)

    async def _detect_idea_dormant(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None,
    ) -> tuple[int, int]:
        """Active ideas not revisited in 30+ days. Suggested actions:
        revive, abandon, convert-to-task. The revive path is what an agent
        would surface in a daily digest ("you had this idea last month;
        worth a second look?")."""
        async with self._pool.tenant_session(tenant_id) as conn:
            stale = await conn.fetchval(
                """
                WITH live AS (
                  SELECT 'idea_dormant:idea:' || idea_id::text AS sig
                    FROM v_ideas
                   WHERE status = 'active'
                     AND COALESCE(last_revisited_at, originated_at, created_at)
                         < now() - make_interval(days => $1)
                ),
                del AS (
                  DELETE FROM resurface_artifacts
                   WHERE kind = 'predicted_action'
                     AND subject_signature LIKE 'idea_dormant:%'
                     AND NOT EXISTS (
                       SELECT 1 FROM live WHERE live.sig = subject_signature
                     )
                  RETURNING 1
                )
                SELECT count(*) FROM del
                """,
                _IDEA_DORMANT_DAYS,
            ) or 0

            fresh = await conn.fetchval(
                """
                WITH src AS (
                  SELECT
                    idea_id, title, summary, category, priority,
                    COALESCE(last_revisited_at, originated_at, created_at) AS last_touch
                  FROM v_ideas
                  WHERE status = 'active'
                    AND COALESCE(last_revisited_at, originated_at, created_at)
                        < now() - make_interval(days => $2)
                  ORDER BY surface_score DESC NULLS LAST,
                           COALESCE(last_revisited_at, originated_at) ASC
                  LIMIT $3
                )
                INSERT INTO resurface_artifacts (
                  tenant_id, kind, content, related_entity_ids,
                  expires_at, dreaming_run_id, status,
                  requires_approval, subject_signature
                )
                SELECT
                  $1::uuid,
                  'predicted_action'::resurface_artifact_kind,
                  jsonb_build_object(
                    'action_kind',     'idea_dormant',
                    'subject_kind',    'idea',
                    'subject_id',      idea_id,
                    'subject_label',   COALESCE(title, summary),
                    'context', jsonb_build_object(
                      'category',   category,
                      'priority',   priority,
                      'last_touch', last_touch,
                      'days_dormant',
                        EXTRACT(epoch FROM (now() - last_touch))/86400.0
                    ),
                    'suggested_actions', jsonb_build_array(
                      jsonb_build_object('name','revive',          'reversibility','high',   'requires_approval', false),
                      jsonb_build_object('name','convert_to_task', 'reversibility','medium', 'requires_approval', true),
                      jsonb_build_object('name','abandon',         'reversibility','medium', 'requires_approval', true)
                    )
                  ),
                  '{}',
                  now() + ($4::interval),
                  $5::uuid,
                  'drafted'::resurface_artifact_status,
                  false,
                  'idea_dormant:idea:' || idea_id::text
                FROM src
                ON CONFLICT (tenant_id, subject_signature)
                  WHERE kind = 'predicted_action' AND subject_signature IS NOT NULL
                DO UPDATE SET
                  content         = EXCLUDED.content,
                  expires_at      = EXCLUDED.expires_at,
                  dreaming_run_id = EXCLUDED.dreaming_run_id,
                  generated_at    = now()
                RETURNING (SELECT count(*) FROM src)
                """,
                tenant_id, _IDEA_DORMANT_DAYS, _ROW_CAP_PER_KIND,
                _OPPORTUNITY_TTL, dreaming_run_id,
            ) or 0
            return int(stale), int(fresh)

    async def _detect_entity_stale(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None,
    ) -> tuple[int, int]:
        """Entities with significant mention history but no recent
        access. The 'are you still working on X?' opportunity. Min
        mention threshold avoids noise from one-off mentions."""
        async with self._pool.tenant_session(tenant_id) as conn:
            stale = await conn.fetchval(
                """
                WITH live AS (
                  SELECT 'entity_stale:entity:' || entity_id::text AS sig
                    FROM v_entities
                   WHERE mention_count >= $1
                     AND COALESCE(last_accessed_at, last_mentioned_at)
                         < now() - make_interval(days => $2)
                ),
                del AS (
                  DELETE FROM resurface_artifacts
                   WHERE kind = 'predicted_action'
                     AND subject_signature LIKE 'entity_stale:%'
                     AND NOT EXISTS (
                       SELECT 1 FROM live WHERE live.sig = subject_signature
                     )
                  RETURNING 1
                )
                SELECT count(*) FROM del
                """,
                _ENTITY_MIN_MENTIONS, _ENTITY_STALE_DAYS,
            ) or 0

            fresh = await conn.fetchval(
                """
                WITH src AS (
                  SELECT
                    entity_id, name, type, mention_count,
                    last_mentioned_at, last_accessed_at,
                    COALESCE(last_accessed_at, last_mentioned_at) AS last_touch
                  FROM v_entities
                  WHERE mention_count >= $2
                    AND COALESCE(last_accessed_at, last_mentioned_at)
                        < now() - make_interval(days => $3)
                  ORDER BY mention_count DESC, last_mentioned_at DESC
                  LIMIT $4
                )
                INSERT INTO resurface_artifacts (
                  tenant_id, kind, content, related_entity_ids,
                  expires_at, dreaming_run_id, status,
                  requires_approval, subject_signature
                )
                SELECT
                  $1::uuid,
                  'predicted_action'::resurface_artifact_kind,
                  jsonb_build_object(
                    'action_kind',     'entity_stale',
                    'subject_kind',    'entity',
                    'subject_id',      entity_id,
                    'subject_label',   name,
                    'context', jsonb_build_object(
                      'entity_type',   type,
                      'mention_count', mention_count,
                      'last_touch',    last_touch,
                      'days_stale',
                        EXTRACT(epoch FROM (now() - last_touch))/86400.0
                    ),
                    'suggested_actions', jsonb_build_array(
                      jsonb_build_object('name','still_active_check',  'reversibility','high', 'requires_approval', false),
                      jsonb_build_object('name','archive_thread',      'reversibility','high', 'requires_approval', true)
                    )
                  ),
                  ARRAY[entity_id]::uuid[],
                  now() + ($5::interval),
                  $6::uuid,
                  'drafted'::resurface_artifact_status,
                  false,
                  'entity_stale:entity:' || entity_id::text
                FROM src
                ON CONFLICT (tenant_id, subject_signature)
                  WHERE kind = 'predicted_action' AND subject_signature IS NOT NULL
                DO UPDATE SET
                  content         = EXCLUDED.content,
                  expires_at      = EXCLUDED.expires_at,
                  dreaming_run_id = EXCLUDED.dreaming_run_id,
                  generated_at    = now()
                RETURNING (SELECT count(*) FROM src)
                """,
                tenant_id, _ENTITY_MIN_MENTIONS, _ENTITY_STALE_DAYS,
                _ROW_CAP_PER_KIND, _OPPORTUNITY_TTL, dreaming_run_id,
            ) or 0
            return int(stale), int(fresh)

    async def _detect_doc_unscoped(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None,
    ) -> tuple[int, int]:
        """Recently ingested documents not yet assigned to any scope.
        Surface so the agent can ask 'want this in scope Y?' or
        suggest a scope based on topic overlap."""
        async with self._pool.tenant_session(tenant_id) as conn:
            stale = await conn.fetchval(
                """
                WITH live AS (
                  SELECT 'doc_unscoped:document:' || doc_id::text AS sig
                    FROM v_documents
                   WHERE (scope_slugs IS NULL OR scope_slugs = '{}')
                     AND created_at > now() - make_interval(days => $1)
                ),
                del AS (
                  DELETE FROM resurface_artifacts
                   WHERE kind = 'predicted_action'
                     AND subject_signature LIKE 'doc_unscoped:%'
                     AND NOT EXISTS (
                       SELECT 1 FROM live WHERE live.sig = subject_signature
                     )
                  RETURNING 1
                )
                SELECT count(*) FROM del
                """,
                _DOC_UNSCOPED_DAYS,
            ) or 0

            fresh = await conn.fetchval(
                """
                WITH src AS (
                  SELECT
                    doc_id, title, filename, processing_profile,
                    topics, created_at, captured_by, source_channel
                  FROM v_documents
                  WHERE (scope_slugs IS NULL OR scope_slugs = '{}')
                    AND created_at > now() - make_interval(days => $2)
                  -- Phase 5a.3.4 Track 2b: agent-captured first (urgent —
                  -- agent forgot to scope), then user-captured by recency.
                  ORDER BY
                    (captured_by = 'agent') DESC,
                    created_at DESC
                  LIMIT $3
                )
                INSERT INTO resurface_artifacts (
                  tenant_id, kind, content, related_chunk_ids,
                  expires_at, dreaming_run_id, status,
                  requires_approval, subject_signature
                )
                SELECT
                  $1::uuid,
                  'predicted_action'::resurface_artifact_kind,
                  jsonb_build_object(
                    'action_kind',     'doc_unscoped',
                    'subject_kind',    'document',
                    'subject_id',      doc_id,
                    'subject_label',   COALESCE(title, filename),
                    -- Phase 5a.3.4 Track 2b: urgency hint based on
                    -- captured_by + age. agent + young = high urgency
                    -- ('agent forgot to scope right now'); user-captured
                    -- = medium ('user knew it was unscoped'); aged-out =
                    -- low (give the user space, don't nag).
                    'urgency',
                      CASE
                        WHEN captured_by = 'agent'
                             AND created_at > now() - interval '24 hours'
                          THEN 'high'
                        WHEN captured_by = 'agent'
                          THEN 'medium'
                        WHEN created_at > now() - interval '24 hours'
                          THEN 'medium'
                        ELSE 'low'
                      END,
                    'context', jsonb_build_object(
                      'processing_profile', processing_profile,
                      'topics',             topics,
                      'created_at',         created_at,
                      'captured_by',        captured_by,
                      'source_channel',     source_channel,
                      'days_unscoped',
                        EXTRACT(epoch FROM (now() - created_at))/86400.0
                    ),
                    'suggested_actions', jsonb_build_array(
                      jsonb_build_object('name','assign_to_scope', 'reversibility','high', 'requires_approval', true),
                      jsonb_build_object('name','create_scope_and_assign', 'reversibility','high', 'requires_approval', true),
                      jsonb_build_object('name','dismiss',         'reversibility','high', 'requires_approval', false)
                    )
                  ),
                  '{}',
                  now() + ($4::interval),
                  $5::uuid,
                  'drafted'::resurface_artifact_status,
                  false,
                  'doc_unscoped:document:' || doc_id::text
                FROM src
                ON CONFLICT (tenant_id, subject_signature)
                  WHERE kind = 'predicted_action' AND subject_signature IS NOT NULL
                DO UPDATE SET
                  content         = EXCLUDED.content,
                  expires_at      = EXCLUDED.expires_at,
                  dreaming_run_id = EXCLUDED.dreaming_run_id,
                  generated_at    = now()
                RETURNING (SELECT count(*) FROM src)
                """,
                tenant_id, _DOC_UNSCOPED_DAYS, _ROW_CAP_PER_KIND,
                _OPPORTUNITY_TTL, dreaming_run_id,
            ) or 0
            return int(stale), int(fresh)
