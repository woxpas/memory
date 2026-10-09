"""DreamingWorker — Phase 5a.3 background scheduler for resurfacing.

Standalone process (mirrors the woxpas-worker pattern): runs three
concurrent loops:

  1. matview_loop      — refreshes v_mention_velocity globally every
                          matview_interval_sec (single SQL statement,
                          covers all tenants)

  2. tenant_loop       — every tenant_interval_sec, iterate active tenants
                          (had timeline activity in last N hours) and run
                          ScoreMaintainer + PatternScorer for each

  3. heartbeat_loop    — periodic "still alive" log line so ops can see
                          the worker is healthy even when no tenants
                          are being scored

Bookkeeping: every per-tenant pass writes a resurface_dreaming_runs row
with trigger='scheduled', timing, and cloud_egress=false (no LLM use in
the 5a.3 baseline — predicted-question pre-caching lands in 5a.3.1 and
will set the flag).

Optional process: the rest of the system functions without it; scores
stale gracefully. Disable via WOXPAS_DREAMING_ENABLED=false. SIGINT
triggers a graceful shutdown — in-flight tenant pass completes, then
loops exit.

Cloud policy hook is in place via the bookkeeping but not exercised
yet — when 5a.3.1 wires up the LLM-using paths (predicted question
synthesis, conflict pre-detection), the same allow_cloud_for_background_jobs
gate from the existing cloud_policy module applies.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from woxpas_resurfacing.observations import PatternDetector
from woxpas_resurfacing.opportunities import OpportunityDetector
from woxpas_resurfacing.question_cache import QuestionPreCacher
from woxpas_resurfacing.scope_reassigner import ScopeReassigner
from woxpas_resurfacing.score_maintainer import ScoreMaintainer
from woxpas_resurfacing.scorer import PatternScorer
from woxpas_resurfacing.settings import DreamingSettings

if TYPE_CHECKING:
    from woxpas_storage import PostgresPool

log = structlog.get_logger(__name__)


class DreamingWorker:
    """Background scheduler. Construct, start(); SIGINT or stop() to shut down."""

    def __init__(
        self,
        *,
        pool:           PostgresPool,
        settings:       DreamingSettings | None = None,
        question_cache: QuestionPreCacher | None = None,
        qdrant=None,                                            # type: ignore[no-untyped-def]
    ) -> None:
        self._pool     = pool
        self._settings = settings or DreamingSettings()
        self._score_maintainer     = ScoreMaintainer(pool=pool)
        self._pattern_scorer       = PatternScorer(pool=pool)
        self._pattern_detector     = PatternDetector(pool=pool)
        self._opportunity_detector = OpportunityDetector(pool=pool)
        # Optional Phase 5a.3.1 cloud-gated predicted-question cache.
        # When None, that loop is skipped — keeps the worker
        # synthesis-free for installs that haven't enabled cloud egress
        # for background jobs (the default sovereign-first stance).
        self._question_cache       = question_cache
        # Phase 5a.3.3: ScopeReassigner needs a QdrantClient. Optional —
        # workers running without qdrant access skip the pass with a log
        # warning. Production worker (cli.py) constructs it.
        self._scope_reassigner = (
            ScopeReassigner(pool=pool, qdrant=qdrant)
            if qdrant is not None else None
        )
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ─── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all loops + block until stop() is called or SIGINT."""
        if not self._settings.enabled:
            log.info("dreaming.disabled_at_startup")
            return

        log.info("dreaming.start",
                 tenant_interval=self._settings.tenant_interval_sec,
                 matview_interval=self._settings.matview_interval_sec,
                 active_window_hours=self._settings.active_tenant_window_hours)

        # Reap orphaned 'running' rows from a previous worker that crashed
        # or was hard-killed before _end_run wrote. Without this, status
        # queries see stale 'running' state forever. 5-minute safety
        # window so we don't accidentally kill a sibling worker's
        # in-flight run during a hot restart.
        await self._reap_orphaned_runs()

        # Initial pass before entering the cadence — get fresh signals
        # immediately on boot.
        await self._refresh_matview()
        await self._score_active_tenants()

        # Spawn loops
        self._tasks = [
            asyncio.create_task(self._matview_loop(),   name="matview_loop"),
            asyncio.create_task(self._tenant_loop(),    name="tenant_loop"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat_loop"),
        ]
        try:
            await self._stop_event.wait()
        finally:
            await self._cancel_tasks()
            log.info("dreaming.stopped")

    async def stop(self) -> None:
        """Signal all loops to exit. Idempotent."""
        self._stop_event.set()

    async def _cancel_tasks(self) -> None:
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _wait_or_stop(self, timeout: float) -> bool:  # noqa: ASYNC109  # timeout is forwarded straight to asyncio.wait_for; a cancel scope buys nothing here
        """asyncio.wait_for(stop_event, timeout=...). Returns True iff
        stop was signaled before the timeout (so the caller exits the
        loop)."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    # ─── loops ─────────────────────────────────────────────────────────────

    async def _matview_loop(self) -> None:
        while not self._stop_event.is_set():
            if await self._wait_or_stop(self._settings.matview_interval_sec):
                break
            try:
                await self._refresh_matview()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("dreaming.matview_refresh_failed",
                          error_class=type(e).__name__,
                          error=str(e)[:200])

    async def _tenant_loop(self) -> None:
        while not self._stop_event.is_set():
            if await self._wait_or_stop(self._settings.tenant_interval_sec):
                break
            try:
                await self._score_active_tenants()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("dreaming.tenant_loop_failed",
                          error_class=type(e).__name__,
                          error=str(e)[:200])

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            if await self._wait_or_stop(self._settings.heartbeat_interval_sec):
                break
            log.info("dreaming.heartbeat",
                     tasks=[t.get_name() for t in self._tasks if not t.done()])

    # ─── work ─────────────────────────────────────────────────────────────

    async def _refresh_matview(self) -> None:
        """Refresh v_mention_velocity. CONCURRENTLY is supported because
        we have a unique index on (tenant_id, entity_id) — refresh
        doesn't block readers."""
        t0 = time.monotonic()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "REFRESH MATERIALIZED VIEW CONCURRENTLY v_mention_velocity"
            )
        log.info("dreaming.matview_refreshed",
                 ms=round((time.monotonic() - t0) * 1000, 1))

    async def _list_active_tenants(self) -> list[UUID]:
        """Tenants with timeline activity in the active window. Uses
        the timeline_tenant_time_idx for cheap reads. Returned in
        most-recent-activity-first order so a worker under load gets
        the most-relevant tenants first."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tenant_id, MAX(occurred_at) AS last_at
                  FROM timeline_events
                 WHERE occurred_at > now() - make_interval(
                            hours => $1)
                 GROUP BY tenant_id
                 ORDER BY last_at DESC
                """,
                self._settings.active_tenant_window_hours,
            )
        return [r["tenant_id"] for r in rows]

    async def _score_active_tenants(self) -> None:
        """Iterate active tenants, run ScoreMaintainer + PatternScorer
        for each. Bookkeeping per tenant. Bounded by max_runtime_per_tenant
        — if a tenant exceeds the cap, log + move on (don't block the
        loop)."""
        t_start = time.monotonic()
        active = await self._list_active_tenants()
        log.info("dreaming.tenant_pass_begin",
                 active_count=len(active))

        for tenant_id in active:
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._run_for_tenant(tenant_id),
                    timeout=self._settings.max_runtime_per_tenant_sec,
                )
            except TimeoutError:
                log.warning("dreaming.tenant_timeout",
                            tenant_id=str(tenant_id),
                            cap_sec=self._settings.max_runtime_per_tenant_sec)
            except Exception as e:
                log.error("dreaming.tenant_failed",
                          tenant_id=str(tenant_id),
                          error_class=type(e).__name__,
                          error=str(e)[:200])

        log.info("dreaming.tenant_pass_done",
                 active_count=len(active),
                 ms=round((time.monotonic() - t_start) * 1000, 1))

    async def _run_for_tenant(self, tenant_id: UUID) -> None:
        """One scheduled pass for one tenant: bookkeeping start, score
        maintenance, pattern scoring, bookkeeping end."""
        run_id = await self._start_run(tenant_id)
        try:
            score_counts = await self._score_maintainer.update_all_for_tenant(
                tenant_id=tenant_id,
            )
            pattern_counts = await self._pattern_scorer.score_all(
                tenant_id=tenant_id,
            )
            # Phase 5a.4: derive user preferences from accumulated
            # observations. Cheap pass — typically 5-15ms even with
            # thousands of observations.
            await self._pattern_detector.update_preferences_for_tenant(
                tenant_id=tenant_id,
            )
            # Phase 5a.3.1: surface memory-internal action opportunities
            # (overdue tasks, open conflicts, dormant ideas, stale
            # entities, unscoped docs). Local SQL aggregations only —
            # no LLM, no cloud egress.
            opp_counts = await self._opportunity_detector.detect_for_tenant(
                tenant_id=tenant_id, dreaming_run_id=run_id,
            )

            # Phase 5a.3.3: ScopeReassigner — retry scope auto-assign at
            # a lower threshold for docs that ended up unscoped. Pure
            # scoring loop against existing scopes; no LLM, no cloud.
            scope_counts: dict[str, Any] = {}
            if self._scope_reassigner is not None:
                scope_counts = await self._scope_reassigner.reassign_for_tenant(
                    tenant_id=tenant_id,
                )

            # Phase 5a.3.1: cloud-gated predicted-question pre-cache.
            # Internally checks the per-tenant cloud-for-bg flag; no-op
            # for tenants that haven't opted in. Skipped entirely when
            # the worker wasn't constructed with a question cache.
            qcache_counts: dict[str, Any] = {}
            qcache_tokens = 0
            if self._question_cache is not None:
                qcache_counts = await self._question_cache.precache_for_tenant(
                    tenant_id=tenant_id, dreaming_run_id=run_id,
                )
                qcache_tokens = int(qcache_counts.get("tokens_used") or 0)
            # Sum of positive counts only — sub-routines return -1 to
            # signal failure. Clamp to keep bookkeeping artifact counts
            # honest (never negative).
            def _pos(v) -> int:
                try:
                    return max(0, int(v))
                except (TypeError, ValueError):
                    return 0
            artifacts = (
                _pos(score_counts.get("entities"))
                + _pos(score_counts.get("ideas"))
                + _pos(score_counts.get("documents"))
                + sum(_pos(v) for v in pattern_counts.values())
                + sum(_pos(v) for k, v in opp_counts.items() if k != "ms")
                + _pos(qcache_counts.get("predictions"))
                + _pos(scope_counts.get("reassigned"))
            )
            await self._end_run(run_id, status="succeeded",
                                artifacts_created=artifacts,
                                cloud_tokens_used=qcache_tokens)
        except Exception as e:
            await self._end_run(run_id, status="failed",
                                error_reason=f"{type(e).__name__}: {e}"[:200])
            raise

    # ─── bookkeeping ──────────────────────────────────────────────────────

    async def _reap_orphaned_runs(self) -> None:
        """Flip orphaned 'running' rows to 'cancelled'. Runs at worker
        startup. The 5-minute safety window means a parallel worker's
        in-flight run (started recently) is left untouched; only truly
        stale runs from a crashed predecessor get cleaned."""
        async with self._pool.acquire() as conn:
            n = await conn.fetchval(
                """
                WITH orphaned AS (
                  UPDATE resurface_dreaming_runs
                     SET status       = 'cancelled'::resurface_run_status,
                         ended_at     = COALESCE(ended_at, now()),
                         error_reason = COALESCE(error_reason,
                                                 'orphaned at worker restart')
                   WHERE status = 'running'
                     AND started_at < now() - interval '5 minutes'
                  RETURNING run_id
                )
                SELECT count(*) FROM orphaned
                """
            )
        if n:
            log.warning("dreaming.reaped_orphaned_runs", count=n)

    async def _start_run(self, tenant_id: UUID) -> UUID:
        async with self._pool.tenant_session(tenant_id) as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO resurface_dreaming_runs
                       (tenant_id, trigger, trigger_meta, status)
                VALUES ($1, 'scheduled'::resurface_run_trigger,
                        '{"source": "dreaming_worker"}'::jsonb,
                        'running'::resurface_run_status)
                RETURNING run_id
                """,
                tenant_id,
            )
        return run_id

    async def _end_run(
        self, run_id: UUID, *,
        status:            str,
        artifacts_created: int = 0,
        error_reason:      str | None = None,
        cloud_tokens_used: int = 0,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE resurface_dreaming_runs SET
                    ended_at          = now(),
                    status            = $2::resurface_run_status,
                    artifacts_created = $3,
                    error_reason      = $4,
                    cloud_tokens_used = $5,
                    cloud_egress_used = $5 > 0
                  WHERE run_id = $1
                """,
                run_id, status, artifacts_created, error_reason, cloud_tokens_used,
            )
