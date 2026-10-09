"""Resolve TimeFilter.relative phrases to concrete (since, until) timestamps.

All resolution is UTC-based for predictability across instances. The CLIENT
is responsible for converting to local time when displaying — server reasons
in UTC. (Pillar 4: portable; multi-instance safe.)

Recognized relative phrases (case-insensitive, hyphens or underscores):
    last_hour | last_24_hours | last_day | last_2_days | last_3_days |
    last_week | last_2_weeks | last_month | last_quarter | last_year |
    today | yesterday | this_week | this_month | this_year

Unknown phrases → returns the explicit (since, until) from the filter, or
(None, None) — matches "no time filter".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from woxpas_retrieval.types import TimeFilter


_HOUR  = timedelta(hours=1)
_DAY   = timedelta(days=1)
_WEEK  = timedelta(days=7)
_MONTH = timedelta(days=30)     # approximation — calendar months vary
_QUARTER = timedelta(days=91)
_YEAR  = timedelta(days=365)


def _normalize(phrase: str) -> str:
    return phrase.strip().lower().replace("-", "_").replace(" ", "_")


def resolve_time_filter(
    tf: TimeFilter | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return (since, until). Either may be None to mean unbounded.
    `now` is injectable for testability."""
    if tf is None:
        return None, None
    n = now or datetime.now(UTC)

    # Explicit since/until takes precedence over relative.
    if tf.since is not None or tf.until is not None:
        return tf.since, tf.until

    if tf.relative is None:
        return None, None

    rel = _normalize(tf.relative)
    midnight = n.replace(hour=0, minute=0, second=0, microsecond=0)

    match rel:
        case "last_hour":
            return n - _HOUR, n
        case "last_24_hours":
            return n - _DAY, n
        case "last_day":
            return n - _DAY, n
        case "last_2_days":
            return n - 2 * _DAY, n
        case "last_3_days":
            return n - 3 * _DAY, n
        case "last_week":
            return n - _WEEK, n
        case "last_2_weeks":
            return n - 2 * _WEEK, n
        case "last_month":
            return n - _MONTH, n
        case "last_quarter":
            return n - _QUARTER, n
        case "last_year":
            return n - _YEAR, n
        case "today":
            return midnight, midnight + _DAY
        case "yesterday":
            return midnight - _DAY, midnight
        case "this_week":
            # ISO week: Monday is day 0
            start = midnight - timedelta(days=midnight.weekday())
            return start, start + _WEEK
        case "this_month":
            start = midnight.replace(day=1)
            return start, n
        case "this_year":
            start = midnight.replace(month=1, day=1)
            return start, n
        # ─── future-relative — for tasks/deadlines + upcoming ─────────────
        case "tomorrow":
            return midnight + _DAY, midnight + 2 * _DAY
        case "next_week":
            return n, n + _WEEK
        case "next_month":
            return n, n + _MONTH
        case _:
            # Unknown phrase — return None,None so caller treats as "no filter".
            return None, None
