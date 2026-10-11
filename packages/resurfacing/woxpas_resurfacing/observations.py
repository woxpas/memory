"""PatternDetector — derives user preferences from agent observations.

Phase 5a.4 component. Walks `resurface_observations` for a tenant and
updates `resurface_user_preferences` based on detected patterns.

v0.1 patterns (cheap, high-signal):
  - answer_length_preference: dismissal heuristic on long answers
  - most_active_hour: peak observation hour (cross-agent)
  - most_active_day_of_week: peak weekday (cross-agent)
  - top_tools_used: frequency rank of tool_called over last 30 days
  - top_skills_used: frequency rank of skill_id over last 30 days
  - tool_by_hour:  {hour -> top tools}  (5a.3.1; foundation for adaptive
                   schedules — agents derive cadence from this)
  - skill_by_hour: {hour -> top skills} (5a.3.1)

Deferred to v0.2:
  - citation_density_preference (needs explicit click events)
  - persona_tone learning (needs sentiment/style detection)
  - tool sequencing patterns (frequent A→B chains)
  - purpose-based combinations (requires normalization layer)

The detector runs as part of the dreaming worker's tenant pass, so
preferences refresh on the same 30-min cadence as scoring/patterns.
Agents reading /resurface/preferences see signals updated within that
cadence; observations.observation_count_30d tells the agent how
"warmed up" the preferences are.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger(__name__)


# Lookback windows
_OBSERVATION_LOOKBACK_DAYS = 30

# Heuristic thresholds
_DISMISSAL_RATIO_FOR_CONCISE = 0.6  # 60%+ of long answers dismissed → concise
_LONG_ANSWER_TOKEN_THRESHOLD = 500  # output tokens above this counts as "long"
_MIN_LONG_ANSWERS_FOR_DECISION = 5  # need 5+ data points to flip preference
_TOP_TOOLS_LIMIT             = 5


class PatternDetector:
    """Derives user_preferences from observations. Tenant-isolated via
    tenant_session. Idempotent — running twice in a row produces the
    same row in resurface_user_preferences (UPSERT)."""

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool

    async def update_preferences_for_tenant(
        self, *, tenant_id: UUID,
    ) -> dict[str, Any]:
        """Run the detection logic + UPSERT preferences. Returns counts
        for bookkeeping."""
        t_start = time.monotonic()

        async with self._pool.tenant_session(tenant_id) as conn:
            # Phase 5a.3.2 fix B: pull tenant timezone so we bucket
            # hours/days into the USER's local time, not the server's UTC.
            tz_name = await conn.fetchval(
                "SELECT timezone FROM tenants WHERE tenant_id = $1",
                tenant_id,
            ) or "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                log.warning("pattern_detector.invalid_tz",
                            tenant_id=str(tenant_id), tz=tz_name)
                tz = ZoneInfo("UTC")

            # Pull last 30 days of observations in one shot. Each
            # detector function reads from this collection.
            obs = await conn.fetch(
                """
                SELECT observation_id, agent_id, observed_at, context,
                       tool_called, skill_id, tool_input, outcome
                  FROM resurface_observations
                 WHERE observed_at > now() - make_interval(days => $1)
                """,
                _OBSERVATION_LOOKBACK_DAYS,
            )

            counts: dict[str, Any] = {"observation_count_30d": len(obs)}

            if not obs:
                # Cold tenant — clear or leave defaults; either way
                # ensure a row exists with default values.
                # Dreaming runs with no member context → writes the team-default
                # row (member_id NULL). One conflict target (migration 034's
                # COALESCE index) covers both member and default rows.
                await conn.execute(
                    """
                    INSERT INTO resurface_user_preferences (tenant_id)
                    VALUES ($1)
                    ON CONFLICT (tenant_id, COALESCE(member_id, '00000000-0000-0000-0000-000000000000'::uuid))
                    DO UPDATE SET updated_at = now()
                    """,
                    tenant_id,
                )
                counts["ms"] = round((time.monotonic() - t_start) * 1000, 1)
                return counts

            # ─── Detect signals ─────────────────────────────────────
            answer_length = self._detect_answer_length(obs)
            most_active_hour = self._detect_most_active_hour(obs, tz=tz)
            most_active_dow  = self._detect_most_active_day_of_week(obs, tz=tz)
            top_tools = self._detect_top_tools(obs)
            top_skills = self._detect_top_skills(obs)
            tool_by_hour  = self._detect_tool_by_hour(obs, tz=tz)
            skill_by_hour = self._detect_skill_by_hour(obs, tz=tz)

            counts["answer_length_preference"] = answer_length
            counts["most_active_hour"] = most_active_hour
            counts["most_active_day_of_week"] = most_active_dow
            counts["top_tools_used"] = top_tools
            counts["top_skills_used"] = top_skills

            tool_use_payload: dict[str, Any] = {
                "top_tools_used":         top_tools,
                "top_skills_used":        top_skills,
                "most_active_hour":       most_active_hour,
                "most_active_day_of_week": most_active_dow,
                "tool_by_hour":           tool_by_hour,
                "skill_by_hour":          skill_by_hour,
                "_phase": "5a.3.1",
                "_observation_count_30d": len(obs),
                "_updated_at": time.time(),
            }

            await conn.execute(
                """
                INSERT INTO resurface_user_preferences (
                    tenant_id,
                    answer_length_preference,
                    tool_use_preferences,
                    updated_at
                )
                VALUES ($1, $2, $3, now())
                ON CONFLICT (tenant_id, COALESCE(member_id, '00000000-0000-0000-0000-000000000000'::uuid))
                DO UPDATE SET
                    answer_length_preference = EXCLUDED.answer_length_preference,
                    tool_use_preferences     = EXCLUDED.tool_use_preferences,
                    updated_at               = now()
                """,
                tenant_id, answer_length, tool_use_payload,
            )

        counts["ms"] = round((time.monotonic() - t_start) * 1000, 1)
        log.info("resurfacing.pattern_detector.done",
                 tenant_id=str(tenant_id), counts=counts)
        return counts

    # ─── Detectors ──────────────────────────────────────────────────────────

    def _detect_answer_length(self, obs: list[Any]) -> str:
        """If user dismissed >60% of long answers, set 'concise'.
        Need ≥5 long-answer data points before flipping preference.

        outcome.user_engaged is True when user kept reading / acted on
        the answer; False when they dismissed/scrolled-past quickly."""
        long_total = 0
        long_dismissed = 0
        for o in obs:
            if o["tool_called"] not in {"memory.ask", "memory.synthesize"}:
                continue
            outcome = o["outcome"] or {}
            tokens = outcome.get("completion_tokens") or outcome.get("tokens") or 0
            try:
                tokens = int(tokens)
            except (TypeError, ValueError):
                tokens = 0
            if tokens < _LONG_ANSWER_TOKEN_THRESHOLD:
                continue
            long_total += 1
            if outcome.get("user_engaged") is False:
                long_dismissed += 1

        if long_total < _MIN_LONG_ANSWERS_FOR_DECISION:
            return "normal"      # not enough data
        if long_dismissed / long_total >= _DISMISSAL_RATIO_FOR_CONCISE:
            return "concise"
        # Future: could detect 'exhaustive' from the inverse (user
        # always engages with long, never with short) — defer to v0.2.
        return "normal"

    def _detect_most_active_hour(
        self, obs: list[Any], *, tz: ZoneInfo,
    ) -> int | None:
        """Peak hour by observation count, in tenant-local time.
        Returns None for low counts (< 10) — too noisy."""
        if len(obs) < 10:
            return None
        hours: Counter[int] = Counter()
        for o in obs:
            ts = o["observed_at"]
            if ts is None:
                continue
            hours[ts.astimezone(tz).hour] += 1
        if not hours:
            return None
        return hours.most_common(1)[0][0]

    def _detect_top_tools(self, obs: list[Any]) -> list[str]:
        """Frequency rank of tool_called over the lookback window."""
        tools: Counter[str] = Counter(o["tool_called"] for o in obs if o["tool_called"])
        return [name for name, _ in tools.most_common(_TOP_TOOLS_LIMIT)]

    def _detect_top_skills(self, obs: list[Any]) -> list[str]:
        """Phase 5a.4.1 — frequency rank of skill_id over the lookback
        window. Skipped observations with skill_id=null (direct tool
        calls). Returns empty list when no skill-aware observations
        exist yet."""
        skills: Counter[str] = Counter(
            o["skill_id"] for o in obs if o["skill_id"]
        )
        return [name for name, _ in skills.most_common(_TOP_TOOLS_LIMIT)]

    def _detect_most_active_day_of_week(
        self, obs: list[Any], *, tz: ZoneInfo,
    ) -> int | None:
        """Peak weekday (0=Monday, 6=Sunday, ISO convention) by
        observation count, in tenant-local time. Returns None for
        low counts (< 10) — too noisy. Foundation for any agent
        (an agent later) deriving a weekly schedule pattern from
        observed behavior."""
        if len(obs) < 10:
            return None
        days: Counter[int] = Counter()
        for o in obs:
            ts = o["observed_at"]
            if ts is None:
                continue
            days[ts.astimezone(tz).weekday()] += 1
        if not days:
            return None
        return days.most_common(1)[0][0]

    def _detect_tool_by_hour(
        self, obs: list[Any], *, tz: ZoneInfo,
    ) -> dict[str, list[str]]:
        """Phase 5a.3.1 — per-hour top tool aggregation in tenant-
        local time. {"9": ["memory.ask", ...], ...}. Hours with
        fewer than 3 observations are dropped (too noisy to trust).

        Foundation for adaptive scheduling: agents (an agent) read this
        to derive WHEN to surface what — e.g. "user uses memory.ask
        most at 9am LOCAL" → pre-prepare 9am-likely answers slightly
        before. Memory does NOT do the scheduling; it surfaces the
        signal."""
        per_hour: dict[int, Counter[str]] = {}
        for o in obs:
            ts = o["observed_at"]
            tool = o["tool_called"]
            if ts is None or not tool:
                continue
            per_hour.setdefault(ts.astimezone(tz).hour, Counter())[tool] += 1

        out: dict[str, list[str]] = {}
        for hour, c in per_hour.items():
            if sum(c.values()) < 3:
                continue
            out[str(hour)] = [t for t, _ in c.most_common(_TOP_TOOLS_LIMIT)]
        return out

    def _detect_skill_by_hour(
        self, obs: list[Any], *, tz: ZoneInfo,
    ) -> dict[str, list[str]]:
        """Phase 5a.3.1 — per-hour top skill aggregation in tenant-
        local time. Same shape as tool_by_hour but keyed on skill_id
        (null skills dropped). Empty when no skill-aware observations
        exist."""
        per_hour: dict[int, Counter[str]] = {}
        for o in obs:
            ts = o["observed_at"]
            skill = o["skill_id"]
            if ts is None or not skill:
                continue
            per_hour.setdefault(ts.astimezone(tz).hour, Counter())[skill] += 1

        out: dict[str, list[str]] = {}
        for hour, c in per_hour.items():
            if sum(c.values()) < 3:
                continue
            out[str(hour)] = [s for s, _ in c.most_common(_TOP_TOOLS_LIMIT)]
        return out
