"""PatternScorer — Dormant Brain pattern signals.

Walks the v_* views + v_mention_velocity matview and writes scored
pattern signals to resurface_patterns_cache. Five kinds:

  trending   — entity mention rate rising (slope > 0.3)
  fading     — entity mention rate declining (slope < -0.3)
  forgotten  — idea/task created but never engaged (>30d, last_*_at NULL)
  recurring  — entity mentioned 3+ times across 3+ distinct days/30d
  attention  — overdue tasks + open conflicts (composite, user-actionable)

Each scoring method is independent — failures in one don't block others.
The score_all() orchestrator catches per-kind exceptions and logs them.

Phase 5a.2 ships the scorer + manual invocation. Phase 5a.3's dreaming
worker takes ownership of automatic scheduled invocation (every N hours).
For v0.1 of resurfacing, the API endpoint can also trigger sync
computation when the cache is empty (cold start).

Pattern TTLs (configurable via _PATTERN_TTLS):
  trending/fading:  6h   (mention rates change quickly)
  forgotten:        24h  (slow-moving signal)
  recurring:        12h  (cadence detection is moderately fresh)
  attention:        2h   (overdue/conflict state is fast-moving)
"""

from __future__ import annotations

import time
from datetime import timedelta
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────

_TRENDING_THRESHOLD     = 0.3      # slope minimum to count as trending
_FADING_THRESHOLD       = -0.3     # slope below this = fading
_FORGOTTEN_AGE_DAYS     = 30       # ignored items older than this trigger
_RECURRING_MIN_MENTIONS = 3
_RECURRING_MIN_DAYS     = 3
_RECURRING_WINDOW_DAYS  = 30

_PATTERN_TTLS = {
    "trending":  timedelta(hours=6),
    "fading":    timedelta(hours=6),
    "forgotten": timedelta(hours=24),
    "recurring": timedelta(hours=12),
    "attention": timedelta(hours=2),
}

_ROW_CAP_PER_KIND = 50          # cap rows written per (tenant, kind)


# ─── Scorer ─────────────────────────────────────────────────────────────────

class PatternScorer:
    """Scores patterns and upserts into resurface_patterns_cache.

    Reads via memory's view layer; writes to the resurfacing-owned table.
    Tenant-isolated via tenant_session.
    """

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool

    async def score_all(self, *, tenant_id: UUID) -> dict[str, int]:
        """Run all five scorers for one tenant. Returns per-kind row
        counts. Per-kind exceptions are caught + logged so a single
        failure doesn't poison the whole pass."""
        t_start = time.monotonic()
        counts: dict[str, int] = {}
        scorers = (
            ("trending",  self._score_trending),
            ("fading",    self._score_fading),
            ("forgotten", self._score_forgotten),
            ("recurring", self._score_recurring),
            ("attention", self._score_attention),
        )
        for kind, fn in scorers:
            try:
                n = await fn(tenant_id)
                counts[kind] = n
            except Exception as e:
                log.error("resurfacing.scorer.failed",
                          kind=kind, tenant_id=str(tenant_id),
                          error_class=type(e).__name__, error=str(e)[:200])
                counts[kind] = -1
        log.info("resurfacing.scorer.done",
                 tenant_id=str(tenant_id),
                 counts=counts,
                 ms=round((time.monotonic() - t_start) * 1000, 1))
        return counts

    # ─── per-kind scorers ──────────────────────────────────────────────────

    async def _score_trending(self, tenant_id: UUID) -> int:
        """Entities whose 24h mention rate exceeds their 30-day baseline.
        slope_24h_vs_baseline is in [-1, 1]; we keep entities with slope
        > _TRENDING_THRESHOLD."""
        ttl_seconds = int(_PATTERN_TTLS["trending"].total_seconds())
        async with self._pool.tenant_session(tenant_id) as conn:
            # Ensure matview is reasonably fresh — refresh if older than ttl/2.
            # Cheap when data hasn't changed; safe to call.
            await self._maybe_refresh_velocity(conn)

            # Note: v_mention_velocity is matview, not RLS-enforced.
            # Filter explicitly by tenant_id.
            n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1                                AS tenant_id,
                    'trending'::resurface_pattern_kind,
                    'entity'::resurface_subject_kind,
                    mv.entity_id,
                    mv.slope_24h_vs_baseline           AS score,
                    format(
                        '%s mentions in last 24h vs %s in last 7d (slope %s)',
                        mv.mentions_24h, mv.mentions_7d,
                        round(mv.slope_24h_vs_baseline::numeric, 2)::text
                    ),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_mention_velocity mv
                 WHERE mv.tenant_id = $1
                   AND mv.slope_24h_vs_baseline > $3
                 ORDER BY mv.slope_24h_vs_baseline DESC
                 LIMIT $4
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING (SELECT count(*) FROM resurface_patterns_cache
                            WHERE tenant_id = $1 AND kind = 'trending')
                """,
                tenant_id, ttl_seconds,
                _TRENDING_THRESHOLD, _ROW_CAP_PER_KIND,
            )
        return int(n or 0)

    async def _score_fading(self, tenant_id: UUID) -> int:
        """Entities going quiet — slope below threshold. Score is the
        ABSOLUTE magnitude of decline (so high score = strongly fading)."""
        ttl_seconds = int(_PATTERN_TTLS["fading"].total_seconds())
        async with self._pool.tenant_session(tenant_id) as conn:
            await self._maybe_refresh_velocity(conn)
            n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1, 'fading'::resurface_pattern_kind,
                    'entity'::resurface_subject_kind, mv.entity_id,
                    -mv.slope_24h_vs_baseline,
                    format(
                        'mentioned %s× in last 7d, only %s in last 24h (slope %s)',
                        mv.mentions_7d, mv.mentions_24h,
                        round(mv.slope_24h_vs_baseline::numeric, 2)::text
                    ),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_mention_velocity mv
                 WHERE mv.tenant_id = $1
                   AND mv.slope_24h_vs_baseline < $3
                   AND mv.mentions_30d > 0       -- entity must have been mentioned at all
                 ORDER BY mv.slope_24h_vs_baseline ASC
                 LIMIT $4
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING (SELECT count(*) FROM resurface_patterns_cache
                            WHERE tenant_id = $1 AND kind = 'fading')
                """,
                tenant_id, ttl_seconds,
                _FADING_THRESHOLD, _ROW_CAP_PER_KIND,
            )
        return int(n or 0)

    async def _score_forgotten(self, tenant_id: UUID) -> int:
        """Ideas + tasks that were created but never engaged with. The
        agent should surface these for "want me to revive any of these?"
        prompts. Score = age in days (older = higher score)."""
        ttl_seconds = int(_PATTERN_TTLS["forgotten"].total_seconds())
        async with self._pool.tenant_session(tenant_id) as conn:
            # Forgotten ideas: status not abandoned/realized, no engagement
            ideas_n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1, 'forgotten'::resurface_pattern_kind,
                    'idea'::resurface_subject_kind, idea_id,
                    EXTRACT(EPOCH FROM (now() - originated_at)) / 86400.0,
                    format('idea %s, never engaged since %s', status,
                           to_char(originated_at, 'YYYY-MM-DD')),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_ideas
                 WHERE last_revisited_at IS NULL
                   AND last_accessed_at IS NULL
                   AND status NOT IN ('abandoned', 'realized')
                   AND originated_at < now() - make_interval(days => $3)
                 ORDER BY originated_at ASC
                 LIMIT $4
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING 1
                """,
                tenant_id, ttl_seconds,
                _FORGOTTEN_AGE_DAYS, _ROW_CAP_PER_KIND,
            )
            # Forgotten tasks: not done/cancelled, never accessed since creation
            tasks_n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1, 'forgotten'::resurface_pattern_kind,
                    'task'::resurface_subject_kind, task_id,
                    EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0,
                    format('task %s, untouched since %s', status,
                           to_char(created_at, 'YYYY-MM-DD')),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_tasks
                 WHERE last_accessed_at IS NULL
                   AND status NOT IN ('done', 'cancelled')
                   AND created_at < now() - make_interval(days => $3)
                 ORDER BY created_at ASC
                 LIMIT $4
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING 1
                """,
                tenant_id, ttl_seconds,
                _FORGOTTEN_AGE_DAYS, _ROW_CAP_PER_KIND,
            )
        return (ideas_n or 0) + (tasks_n or 0)

    async def _score_recurring(self, tenant_id: UUID) -> int:
        """Entities mentioned in a regular cadence — for v0.1, simple
        version: 3+ mentions across 3+ distinct days in last 30 days.
        Score = mentions_count / distinct_days_count (denser cadence
        scores higher).

        v0.2 will add real cadence detection (intervals stddev, FFT-style
        period detection). For now, this catches the obvious recurrings."""
        ttl_seconds = int(_PATTERN_TTLS["recurring"].total_seconds())
        async with self._pool.tenant_session(tenant_id) as conn:
            n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1, 'recurring'::resurface_pattern_kind,
                    'entity'::resurface_subject_kind, e.entity_id,
                    rec.mention_count::double precision / rec.distinct_days,
                    format('mentioned %s× across %s distinct days in last %s days',
                           rec.mention_count, rec.distinct_days, ($3::int)::text),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_entities e
                  JOIN (
                    SELECT
                        CASE WHEN subject_entity_id IS NOT NULL THEN subject_entity_id
                             ELSE object_entity_id END AS entity_id,
                        COUNT(*)                       AS mention_count,
                        COUNT(DISTINCT DATE(created_at)) AS distinct_days
                      FROM v_relations
                     WHERE created_at > now() - make_interval(days => $3::int)
                       AND archived_at IS NULL
                     GROUP BY 1
                    HAVING COUNT(*) >= $4
                       AND COUNT(DISTINCT DATE(created_at)) >= $5
                  ) AS rec ON rec.entity_id = e.entity_id
                 ORDER BY (rec.mention_count::double precision / rec.distinct_days) DESC
                 LIMIT $6
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING 1
                """,
                tenant_id, ttl_seconds,
                _RECURRING_WINDOW_DAYS,                  # $3 — both window + format reuse
                _RECURRING_MIN_MENTIONS, _RECURRING_MIN_DAYS,  # $4, $5
                _ROW_CAP_PER_KIND,                       # $6
            )
        return int(n or 0)

    async def _score_attention(self, tenant_id: UUID) -> int:
        """Composite signal: things the user should pay attention to.
        v0.1 sources: overdue tasks + open conflicts (severity-weighted).
        Score scales with urgency."""
        ttl_seconds = int(_PATTERN_TTLS["attention"].total_seconds())
        async with self._pool.tenant_session(tenant_id) as conn:
            # Overdue tasks: score = days overdue, capped
            tasks_n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1, 'attention'::resurface_pattern_kind,
                    'task'::resurface_subject_kind, task_id,
                    LEAST(30.0, EXTRACT(EPOCH FROM (now() - due_at)) / 86400.0),
                    format('overdue task: %s (was due %s)',
                           title, to_char(due_at, 'YYYY-MM-DD')),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_tasks
                 WHERE is_overdue
                 ORDER BY due_at ASC
                 LIMIT $3
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING 1
                """,
                tenant_id, ttl_seconds, _ROW_CAP_PER_KIND,
            )
            # Open conflicts: score = severity weight × age in days, capped
            conflicts_n = await conn.fetchval(
                """
                INSERT INTO resurface_patterns_cache (
                    tenant_id, kind, subject_kind, subject_id,
                    score, reason, computed_at, ttl_at
                )
                SELECT
                    $1, 'attention'::resurface_pattern_kind,
                    'conflict'::resurface_subject_kind,
                    conflict_id,
                    CASE severity
                        WHEN 'critical' THEN 4.0
                        WHEN 'high'     THEN 3.0
                        WHEN 'medium'   THEN 2.0
                        ELSE                 1.0
                    END * LEAST(7.0,
                        EXTRACT(EPOCH FROM (now() - detected_at)) / 86400.0),
                    format('%s conflict, %s severity, open since %s',
                           kind, severity, to_char(detected_at, 'YYYY-MM-DD')),
                    now(),
                    now() + make_interval(secs => $2)
                  FROM v_conflicts_open
                 ORDER BY severity DESC, detected_at ASC
                 LIMIT $3
                ON CONFLICT (tenant_id, kind, subject_kind, subject_id)
                DO UPDATE SET
                    score       = EXCLUDED.score,
                    reason      = EXCLUDED.reason,
                    computed_at = EXCLUDED.computed_at,
                    ttl_at      = EXCLUDED.ttl_at
                RETURNING 1
                """,
                tenant_id, ttl_seconds, _ROW_CAP_PER_KIND,
            )
        return (tasks_n or 0) + (conflicts_n or 0)

    # ─── helpers ───────────────────────────────────────────────────────────

    async def _maybe_refresh_velocity(self, conn) -> None:
        """Refresh v_mention_velocity matview if its computed_at is older
        than half the trending TTL. Cheap if data hasn't changed; the
        materialized view's REFRESH supports CONCURRENTLY when there's
        a unique index (we have one)."""
        # Read most-recent computed_at; matview returns rows-with-dates.
        # If the matview is empty or all rows are stale, refresh.
        row = await conn.fetchrow(
            "SELECT max(computed_at) AS last_at FROM v_mention_velocity"
        )
        last_at = row["last_at"] if row else None
        # Refresh threshold: half the trending TTL (3h)
        if last_at is None:
            await self._refresh_velocity(conn)
            return
        # Postgres comparison is timezone-aware; safe to compare directly.
        await conn.execute(
            """
            DO $$
            DECLARE
              last_at timestamptz;
            BEGIN
              SELECT max(computed_at) INTO last_at FROM v_mention_velocity;
              IF last_at IS NULL OR last_at < now() - interval '3 hours' THEN
                REFRESH MATERIALIZED VIEW CONCURRENTLY v_mention_velocity;
              END IF;
            END $$;
            """
        )

    async def _refresh_velocity(self, conn) -> None:
        """Force-refresh the velocity matview. Runs CONCURRENTLY to avoid
        blocking readers."""
        await conn.execute(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY v_mention_velocity"
        )
