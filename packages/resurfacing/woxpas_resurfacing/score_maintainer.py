"""ScoreMaintainer — refreshes surface_score / decay_score / momentum_score
on entities, ideas, documents.

Wires up the v0.1 schema commitment that was never honored. The score
columns existed but no code populated them; ordering fell back to
mention_count + recency. With ScoreMaintainer running periodically, the
thinking-brain endpoint reduces to `ORDER BY surface_score DESC` — an
indexed sub-millisecond query at any corpus size.

Composite formulas (see `_score_*` methods for exact math):
  recency       = exp(-age_days / decay_constant)   ∈ (0, 1]
  velocity      = max(0, slope from v_mention_velocity) ∈ [0, 1]
  decay_score   = 1 - recency                       ∈ [0, 1)  (1 = forgotten)
  momentum      = recent_count - older_count, normalized
  surface_score = weighted blend + attention boost  ∈ [0, ~1.5]

Phase 5a.2.1 ships the maintainer. Phase 5a.3 wires it into the dreaming
worker schedule (every 30 min during active hours). For now, callers
trigger via `update_all_for_tenant()`.

The maintainer is bounded — it operates only on rows mentioned/revisited
in the last 90 days (entities/ideas) or accessed in the last 90 days
(documents). Cold rows keep their last computed score; eventual eviction
is a v0.2 cleanup task.

All updates are wrapped in tenant_session for RLS. Per-row UPDATEs
batched via UPDATE … FROM (VALUES …) for efficiency.
"""

from __future__ import annotations

import math
import time
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────

# Decay constants — set so the recency score halves at each constant.
# 14 days = 2-week half-life for entities, 30 days for ideas (longer-lived
# concepts), 14 days for documents (access-driven).
_ENTITY_DECAY_DAYS   = 14.0
_IDEA_DECAY_DAYS     = 30.0
_DOCUMENT_DECAY_DAYS = 14.0

# Window for "active" rows — outside this, scores aren't recomputed
# (avoid touching every entity in a 1M-entity tenant on every run).
_ACTIVE_WINDOW_DAYS = 90

# Composite weights. Sum doesn't need to equal 1.0 — the scoring is
# rank-based, not probabilistic; weights just shape relative emphasis.
#
# Phase 5a.2.2 expands signals beyond just mention/access:
#   - user_feedback (timeline surface.* events) is the gold standard
#     user-interest signal; weighted higher than passive access counts
#   - longevity_factor rewards stable old entities still getting attention
#   - ownership_factor rewards entities that "own" active ideas/tasks
#   - processing_profile_boost gives identity_seed entities priority
#   - severity-weighted conflict attention (vs flat per-conflict bonus)
_W_MENTION_RECENCY     = 0.25   # last_mentioned_at exp-decay (system signal)
_W_ACCESS_RECENCY      = 0.20   # last_accessed_at exp-decay (passive user signal)
_W_USER_FEEDBACK       = 0.15   # surface.acted - dismissed*1.5 - snoozed*0.5 (active user signal)
_W_VELOCITY            = 0.15   # mention_count slope (rising/falling)
_W_CENTRALITY          = 0.10   # graph centrality
_W_LONGEVITY           = 0.05   # log-normalized entity age
_W_OWNERSHIP           = 0.05   # active ideas/tasks owned (capped)
_W_TOTALS              = 0.05   # log-norm of mentions + accesses (combined)

# Severity-weighted conflict boost. Per-conflict additive contribution.
_CONFLICT_SEVERITY_WEIGHT = {
    "critical": 0.30,
    "high":     0.20,
    "medium":   0.10,
    "low":      0.05,
}

# Processing-profile boost — identity_seed entities are "about me" facts,
# explicitly user-asserted, so prioritize.
_PROCESSING_PROFILE_BOOST = {
    "identity_seed": 0.20,
    "personal":      0.05,
    # other profiles default to 0 (no penalty, just no boost)
}

# User-feedback weights. Negative penalties weighted heavier than positive
# rewards because explicit dismissal is a STRONGER signal than passive
# action (a user clicking through is often inattentive; dismissal is
# deliberate).
_FEEDBACK_ACTED_WEIGHT     = 1.0
_FEEDBACK_DISMISSED_WEIGHT = -1.5
_FEEDBACK_SNOOZED_WEIGHT   = -0.5
_FEEDBACK_LOOKBACK_DAYS    = 30
# Floor on user_feedback contribution — even maximally-disliked entities
# don't get fully zeroed (let other signals still surface them in case
# the dismissal context was specific). Floor = -0.5 means feedback can
# subtract up to half a unit from surface_score.
_USER_FEEDBACK_FLOOR = -0.5

# Longevity: entities older than this are "well-established"; saturates
# the longevity factor. Uses a log curve so the gain tapers off.
_LONGEVITY_SATURATION_DAYS = 365.0

# Ownership: active ideas + active tasks an entity owns. Capped to avoid
# one prolific owner dominating the ranking.
_OWNERSHIP_PER_ITEM = 0.05
_OWNERSHIP_CAP      = 0.20


# ─── ScoreMaintainer ───────────────────────────────────────────────────────

class ScoreMaintainer:
    """Refreshes the surfacing-score columns on entities, ideas, documents.

    Tenant-isolated via tenant_session. Each subroutine is independent —
    failure in one (e.g., ideas update fails on an unexpected row) doesn't
    block the others.
    """

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool

    async def update_all_for_tenant(
        self, *, tenant_id: UUID,
    ) -> dict[str, Any]:
        """Refresh entities + ideas + documents for one tenant.
        Returns per-class row counts + total time."""
        t_start = time.monotonic()
        counts: dict[str, int | float] = {}

        for label, fn in (
            ("entities",  self._update_entity_scores),
            ("ideas",     self._update_idea_scores),
            ("documents", self._update_document_scores),
        ):
            try:
                counts[label] = await fn(tenant_id)
            except Exception as e:
                log.error("score_maintainer.failed",
                          class_=label, tenant_id=str(tenant_id),
                          error_class=type(e).__name__,
                          error=str(e)[:200])
                counts[label] = -1

        counts["total_ms"] = round((time.monotonic() - t_start) * 1000, 1)
        log.info("score_maintainer.done",
                 tenant_id=str(tenant_id), counts=counts)
        return counts

    # ─── Per-class subroutines ─────────────────────────────────────────────

    async def _update_entity_scores(self, tenant_id: UUID) -> int:
        """For each entity mentioned/accessed in the active window:
          mention_recency = exp(-mention_age_days / 14)
          access_recency  = exp(-access_age_days  / 14)   -- USER-INTEREST signal
          velocity        = max(0, slope from v_mention_velocity)
          centrality      = existing centrality_score (graph centrality)
          mention_total   = log(1 + mention_count) / log(101)
          access_total    = log(1 + access_count)  / log(101)
          attention_boost = 0.15 × open_conflict_count
          decay_score     = 1 - mention_recency
          momentum_score  = velocity
          surface_score   = weighted blend (see _W_* constants) + attention
        """
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    e.entity_id,
                    e.mention_count,
                    e.access_count,
                    e.first_mentioned_at,                      -- longevity
                    e.last_mentioned_at,
                    e.last_accessed_at,
                    e.centrality_score,
                    COALESCE(mv.slope_24h_vs_baseline, 0)::double precision AS slope,

                    -- Severity-weighted conflict attention (Phase 5a.2.2 #7)
                    COALESCE((
                      SELECT jsonb_object_agg(severity, n)::jsonb
                        FROM (
                          SELECT severity, count(*) AS n
                            FROM conflicts c
                           WHERE c.tenant_id = e.tenant_id
                             AND e.entity_id = ANY (c.subject_entity_ids)
                             AND c.resolved_state = 'open'
                           GROUP BY severity
                        ) AS s
                    ), '{}'::jsonb)                              AS conflicts_by_severity,

                    -- Ownership (Phase 5a.2.2 #5): count active ideas + tasks
                    -- where this entity is the owner.
                    (SELECT count(*) FROM ideas i
                       WHERE i.tenant_id = e.tenant_id
                         AND i.owner_entity_id = e.entity_id
                         AND i.status NOT IN ('abandoned','realized'))
                    +
                    (SELECT count(*) FROM tasks_deadlines t
                       WHERE t.tenant_id = e.tenant_id
                         AND t.owner_entity_id = e.entity_id
                         AND t.status NOT IN ('done','cancelled'))
                                                                AS active_owned_count,

                    -- Processing-profile boost (Phase 5a.2.2 #10): does this
                    -- entity appear in identity_seed or personal docs?
                    (SELECT MAX(
                      CASE d.processing_profile
                        WHEN 'identity_seed' THEN 2
                        WHEN 'personal'      THEN 1
                        ELSE                      0
                      END)
                       FROM relations r
                       JOIN documents d ON d.tenant_id = r.tenant_id
                                       AND d.doc_id    = r.doc_id
                                       AND d.deleted_at IS NULL
                                       AND d.archived_at IS NULL
                      WHERE r.tenant_id = e.tenant_id
                        AND (r.subject_entity_id = e.entity_id
                          OR r.object_entity_id  = e.entity_id))
                                                                AS profile_priority,

                    -- User-feedback signal (Phase 5a.2.2 #3): aggregate
                    -- timeline events. Uses subquery counts so we get one
                    -- row per entity even when no events exist (NULL → 0).
                    (SELECT count(*) FROM timeline_events te
                       WHERE te.tenant_id = e.tenant_id
                         AND te.entity_id = e.entity_id
                         AND te.event_type = 'surface.acted'
                         AND te.occurred_at > now() - make_interval(days => $3))
                                                                AS surface_acted_count,
                    (SELECT count(*) FROM timeline_events te
                       WHERE te.tenant_id = e.tenant_id
                         AND te.entity_id = e.entity_id
                         AND te.event_type = 'surface.dismissed'
                         AND te.occurred_at > now() - make_interval(days => $3))
                                                                AS surface_dismissed_count,
                    (SELECT count(*) FROM timeline_events te
                       WHERE te.tenant_id = e.tenant_id
                         AND te.entity_id = e.entity_id
                         AND te.event_type = 'surface.snoozed'
                         AND te.occurred_at > now() - make_interval(days => $3))
                                                                AS surface_snoozed_count,
                    (SELECT count(*) FROM timeline_events te
                       WHERE te.tenant_id = e.tenant_id
                         AND te.entity_id = e.entity_id
                         AND te.event_type = 'surface.shown'
                         AND te.occurred_at > now() - make_interval(days => $3))
                                                                AS surface_shown_count

                  FROM entities e
                  LEFT JOIN v_mention_velocity mv
                         ON mv.tenant_id = e.tenant_id
                        AND mv.entity_id = e.entity_id
                 WHERE e.tenant_id = $1
                   AND e.merged_into_entity_id IS NULL
                   AND (
                        e.last_mentioned_at IS NULL
                     OR e.last_mentioned_at > now() - make_interval(days => $2)
                     OR e.last_accessed_at  > now() - make_interval(days => $2)
                   )
                """,
                tenant_id, _ACTIVE_WINDOW_DAYS, _FEEDBACK_LOOKBACK_DAYS,
            )

            if not rows:
                return 0

            updates: list[tuple[UUID, float, float, float]] = []
            now_ts = time.time()
            for r in rows:
                # ── recency signals (decay over time)
                m_at = r["last_mentioned_at"]
                m_recency = math.exp(
                    -((now_ts - m_at.timestamp()) / 86400.0) / _ENTITY_DECAY_DAYS
                ) if m_at else 0.0

                a_at = r["last_accessed_at"]
                a_recency = math.exp(
                    -((now_ts - a_at.timestamp()) / 86400.0) / _ENTITY_DECAY_DAYS
                ) if a_at else 0.0

                # ── velocity / centrality
                velocity   = max(0.0, min(1.0, float(r["slope"] or 0.0)))
                centrality = max(0.0, min(1.0, float(r["centrality_score"] or 0.0)))

                # ── total counts (combined into single signal)
                mc = int(r["mention_count"] or 0)
                ac = int(r["access_count"] or 0)
                m_total = min(1.0, math.log1p(mc) / math.log(101)) if mc > 0 else 0.0
                a_total = min(1.0, math.log1p(ac) / math.log(101)) if ac > 0 else 0.0
                totals = (m_total + a_total) / 2.0

                # ── longevity (log curve to saturation)
                first_at = r["first_mentioned_at"]
                if first_at is None:
                    longevity = 0.0
                else:
                    age_days = (now_ts - first_at.timestamp()) / 86400.0
                    longevity = min(1.0,
                        math.log1p(age_days) / math.log1p(_LONGEVITY_SATURATION_DAYS))

                # ── ownership (capped per-entity bonus)
                owned = int(r["active_owned_count"] or 0)
                ownership = min(_OWNERSHIP_CAP, owned * _OWNERSHIP_PER_ITEM)

                # ── user feedback (timeline surface.* events; normalized per-shown)
                shown = int(r["surface_shown_count"] or 0)
                if shown > 0:
                    raw = (
                        _FEEDBACK_ACTED_WEIGHT     * int(r["surface_acted_count"] or 0)
                        + _FEEDBACK_DISMISSED_WEIGHT * int(r["surface_dismissed_count"] or 0)
                        + _FEEDBACK_SNOOZED_WEIGHT   * int(r["surface_snoozed_count"] or 0)
                    )
                    user_feedback = max(_USER_FEEDBACK_FLOOR, raw / shown)
                else:
                    user_feedback = 0.0

                # ── severity-weighted conflict attention
                cbs = r["conflicts_by_severity"] or {}
                if isinstance(cbs, str):
                    import json as _json
                    cbs = _json.loads(cbs)
                attention_boost = sum(
                    _CONFLICT_SEVERITY_WEIGHT.get(sev, 0.0) * int(n)
                    for sev, n in cbs.items()
                )

                # ── processing_profile boost
                profile_priority = int(r["profile_priority"] or 0)
                if profile_priority == 2:        # identity_seed
                    profile_boost = _PROCESSING_PROFILE_BOOST["identity_seed"]
                elif profile_priority == 1:      # personal
                    profile_boost = _PROCESSING_PROFILE_BOOST["personal"]
                else:
                    profile_boost = 0.0

                # ── composite
                surface = (
                    _W_MENTION_RECENCY * m_recency
                    + _W_ACCESS_RECENCY  * a_recency
                    + _W_USER_FEEDBACK   * user_feedback
                    + _W_VELOCITY        * velocity
                    + _W_CENTRALITY      * centrality
                    + _W_LONGEVITY       * longevity
                    + _W_OWNERSHIP       * ownership
                    + _W_TOTALS          * totals
                    + attention_boost
                    + profile_boost
                )
                decay = 1.0 - m_recency

                updates.append((r["entity_id"], surface, decay, velocity))

            # Batched UPDATE via UNNEST — single round-trip.
            await conn.execute(
                """
                UPDATE entities SET
                    surface_score   = u.surface,
                    decay_score     = u.decay,
                    momentum_score  = u.momentum,
                    updated_at      = now()
                FROM unnest($1::uuid[], $2::real[], $3::real[], $4::real[])
                  AS u(entity_id, surface, decay, momentum)
                WHERE entities.tenant_id = $5
                  AND entities.entity_id = u.entity_id
                """,
                [u[0] for u in updates],
                [float(u[1]) for u in updates],
                [float(u[2]) for u in updates],
                [float(u[3]) for u in updates],
                tenant_id,
            )
            return len(updates)

    async def _update_idea_scores(self, tenant_id: UUID) -> int:
        """For each non-abandoned/non-realized idea, compute scores.
        status_weight gives developing/active a boost over raw/dormant.
        Recency uses last_revisited_at when set, else originated_at.
        """
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    idea_id, status, mention_count, access_count,
                    COALESCE(last_revisited_at, originated_at) AS recency_ts,
                    last_accessed_at
                  FROM ideas
                 WHERE tenant_id = $1
                   AND status NOT IN ('abandoned', 'realized')
                   AND (
                        last_revisited_at > now() - make_interval(days => $2)
                     OR originated_at     > now() - make_interval(days => $2)
                     OR last_accessed_at  > now() - make_interval(days => $2)
                   )
                """,
                tenant_id, _ACTIVE_WINDOW_DAYS,
            )

            if not rows:
                return 0

            status_weights = {
                "developing": 1.0, "active":   1.0,
                "raw":        0.7, "dormant":  0.3,
            }
            updates: list[tuple[UUID, float, float, float]] = []
            now_ts = time.time()
            for r in rows:
                # recency from last revisit (or originated if never revisited)
                ts = r["recency_ts"]
                age_days = (now_ts - ts.timestamp()) / 86400.0 if ts else 9999.0
                recency = math.exp(-age_days / _IDEA_DECAY_DAYS)

                # access recency (independent signal — has the idea been viewed lately)
                last_acc = r["last_accessed_at"]
                if last_acc is None:
                    access_recency = 0.0
                else:
                    age_days_acc = (now_ts - last_acc.timestamp()) / 86400.0
                    access_recency = math.exp(-age_days_acc / _IDEA_DECAY_DAYS)

                # mention + access totals (log-normalized)
                mc = int(r["mention_count"] or 0)
                ac = int(r["access_count"] or 0)
                m_total = min(1.0, math.log1p(mc) / math.log(101)) if mc > 0 else 0.0
                a_total = min(1.0, math.log1p(ac) / math.log(101)) if ac > 0 else 0.0

                # status boost (developing/active outrank dormant/raw)
                sw = status_weights.get(r["status"], 0.5)

                # composite — split mention/access vs recency/access_recency:
                #   recency       (0.30) = revisit recency
                #   access_recency(0.25) = USER-INTEREST signal
                #   mention_total (0.20) = cumulative mentions
                #   access_total  (0.25) = cumulative user inspections
                surface = sw * (
                    0.30 * recency
                    + 0.25 * access_recency
                    + 0.20 * m_total
                    + 0.25 * a_total
                )
                decay = 1.0 - recency
                momentum = access_recency      # recent access = momentum

                updates.append((r["idea_id"], surface, decay, momentum))

            await conn.execute(
                """
                UPDATE ideas SET
                    surface_score   = u.surface,
                    decay_score     = u.decay,
                    momentum_score  = u.momentum,
                    updated_at      = now()
                FROM unnest($1::uuid[], $2::real[], $3::real[], $4::real[])
                  AS u(idea_id, surface, decay, momentum)
                WHERE ideas.tenant_id = $5
                  AND ideas.idea_id = u.idea_id
                """,
                [u[0] for u in updates],
                [float(u[1]) for u in updates],
                [float(u[2]) for u in updates],
                [float(u[3]) for u in updates],
                tenant_id,
            )
            return len(updates)

    async def _update_document_scores(self, tenant_id: UUID) -> int:
        """Documents: recency × access_count_normalized."""
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT doc_id, last_accessed_at, access_count
                  FROM documents
                 WHERE tenant_id = $1
                   AND deleted_at IS NULL
                   AND (
                        last_accessed_at > now() - make_interval(days => $2)
                     OR created_at        > now() - make_interval(days => $2)
                   )
                """,
                tenant_id, _ACTIVE_WINDOW_DAYS,
            )
            if not rows:
                return 0

            updates: list[tuple[UUID, float, float, float]] = []
            now_ts = time.time()
            for r in rows:
                last_acc = r["last_accessed_at"]
                if last_acc is None:
                    recency = 0.0
                else:
                    age_days = (now_ts - last_acc.timestamp()) / 86400.0
                    recency = math.exp(-age_days / _DOCUMENT_DECAY_DAYS)

                ac = int(r["access_count"] or 0)
                access_norm = math.log1p(ac) / math.log(51) if ac > 0 else 0.0
                access_norm = min(1.0, access_norm)

                surface = 0.6 * recency + 0.4 * access_norm
                decay = 1.0 - recency
                momentum = access_norm

                updates.append((r["doc_id"], surface, decay, momentum))

            await conn.execute(
                """
                UPDATE documents SET
                    surface_score   = u.surface,
                    decay_score     = u.decay,
                    momentum_score  = u.momentum,
                    updated_at      = now()
                FROM unnest($1::uuid[], $2::real[], $3::real[], $4::real[])
                  AS u(doc_id, surface, decay, momentum)
                WHERE documents.tenant_id = $5
                  AND documents.doc_id = u.doc_id
                """,
                [u[0] for u in updates],
                [float(u[1]) for u in updates],
                [float(u[2]) for u in updates],
                [float(u[3]) for u in updates],
                tenant_id,
            )
            return len(updates)
