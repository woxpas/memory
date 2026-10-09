"""Transactional outbox + LISTEN/NOTIFY publisher.

How this fits together:

  Pipeline writer:
    - In the SAME transaction as state changes, call OutboxRepo.publish(event).
    - The 008 migration's trigger emits NOTIFY woxpas_outbox <outbox_id> on insert.

  Publisher daemon (one per process, runs in a long-lived task):
    - LISTEN woxpas_outbox.
    - On notify (or every poll_interval as fallback), claim a batch via
      SELECT ... FOR UPDATE SKIP LOCKED, dispatch to in-process subscribers,
      mark published or schedule retry.

This gives sub-millisecond local fan-out with durable replay. When we lift to
cloud, swap dispatch() for a NATS/Kafka publish call — same outbox shape.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import asyncpg
import structlog

if TYPE_CHECKING:
    from woxpas_storage.postgres.pool import PostgresPool

from woxpas_core import Event

log = structlog.get_logger(__name__)

Subscriber = Callable[[Event], Awaitable[None]]
"""An async callback registered with the publisher. Raise to trigger retry."""


class OutboxRepo:
    """Single-method publisher used by pipeline stages inside their state-change tx."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def publish(self, event: Event) -> int:
        row = await self._conn.fetchrow(
            """
            INSERT INTO events_outbox (
              tenant_id, event_type, event_version, pipeline_version,
              aggregate_kind, aggregate_id, payload,
              causation_id, correlation_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            RETURNING outbox_id
            """,
            event.tenant_id, event.event_type.value, event.event_version,
            event.pipeline_version, event.aggregate_kind, event.aggregate_id,
            event.payload, event.causation_id, event.correlation_id,
        )
        return cast("int", row["outbox_id"])


class OutboxPublisher:
    """Long-running task that drains events_outbox and dispatches to subscribers."""

    def __init__(
        self,
        pool: PostgresPool,
        *,
        batch_size: int = 64,
        poll_interval_s: float = 1.0,
        visibility_timeout_s: int = 60,
        max_attempts: int = 8,
    ) -> None:
        self._pool = pool
        self._batch_size = batch_size
        self._poll_interval = poll_interval_s
        self._visibility_timeout = visibility_timeout_s
        self._max_attempts = max_attempts
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def subscribe(self, event_type: str | None, callback: Subscriber) -> None:
        """Subscribe to a specific event_type, or pass None for catch-all."""
        key = event_type or "*"
        self._subscribers.setdefault(key, []).append(callback)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="woxpas.outbox.publisher")
        log.info("outbox.publisher.started", batch_size=self._batch_size)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        log.info("outbox.publisher.stopped")

    # ─── internals ────────────────────────────────────────────────────────

    async def _run(self) -> None:
        async with self._listen_for_notifies():
            while not self._stop.is_set():
                drained = await self._drain_once()
                if drained == 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                    except TimeoutError:
                        pass
                self._wake.clear()

    @asynccontextmanager
    async def _listen_for_notifies(self) -> AsyncIterator[None]:
        # A dedicated long-lived connection for LISTEN. Polling fallback covers gaps.
        conn = await asyncpg.connect(dsn=self._pool._settings.dsn())
        try:
            await conn.add_listener("woxpas_outbox", self._on_notify)
            yield
        finally:
            try:
                await conn.remove_listener("woxpas_outbox", self._on_notify)
            finally:
                await conn.close()

    def _on_notify(self, *_: Any) -> None:
        # Wake the drain loop; we don't trust per-message delivery — just use it as a signal.
        self._wake.set()

    async def _drain_once(self) -> int:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT outbox_id, tenant_id, event_type, event_version,
                           pipeline_version, aggregate_kind, aggregate_id,
                           payload, causation_id, correlation_id, attempts
                      FROM events_outbox
                     WHERE published_at IS NULL AND failed_at IS NULL
                       AND scheduled_for <= now() AND available_after <= now()
                     ORDER BY outbox_id
                     LIMIT $1
                     FOR UPDATE SKIP LOCKED
                    """,
                    self._batch_size,
                )
                if not rows:
                    return 0
                # Reserve visibility window so concurrent drainers don't double-fire.
                await conn.execute(
                    """
                    UPDATE events_outbox
                       SET available_after = now() + make_interval(secs => $2),
                           attempts = attempts + 1
                     WHERE outbox_id = ANY($1::bigint[])
                    """,
                    [r["outbox_id"] for r in rows], self._visibility_timeout,
                )
            # Dispatch outside the tx so subscribers don't block other drainers.
            await self._dispatch_batch(rows)
            return len(rows)

    async def _dispatch_batch(self, rows: Sequence[asyncpg.Record]) -> None:
        for row in rows:
            event = self._row_to_event(row)
            try:
                await self._dispatch_one(event)
                await self._mark_published(row["outbox_id"])
            except Exception as e:
                log.warning("outbox.dispatch.failed",
                            event_type=event.event_type.value,
                            outbox_id=row["outbox_id"],
                            attempt=row["attempts"],
                            error=str(e))
                if row["attempts"] >= self._max_attempts:
                    await self._mark_failed(row["outbox_id"], str(e))

    async def _dispatch_one(self, event: Event) -> None:
        subs = (
            self._subscribers.get(event.event_type.value, [])
            + self._subscribers.get("*", [])
        )
        if not subs:
            return
        await asyncio.gather(*(s(event) for s in subs))

    async def _mark_published(self, outbox_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE events_outbox SET published_at = now() WHERE outbox_id = $1",
                outbox_id,
            )

    async def _mark_failed(self, outbox_id: int, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE events_outbox "
                "   SET failed_at = now(), last_error = $2 "
                " WHERE outbox_id = $1",
                outbox_id, error,
            )

    @staticmethod
    def _row_to_event(row: asyncpg.Record) -> Event:
        from woxpas_core import EventType
        return Event(
            event_type=EventType(row["event_type"]),
            event_version=row["event_version"],
            pipeline_version=row["pipeline_version"],
            aggregate_kind=row["aggregate_kind"],
            aggregate_id=row["aggregate_id"],
            tenant_id=row["tenant_id"],
            occurred_at=datetime.fromtimestamp(0),  # unused for dispatch; row carries truth
            correlation_id=row["correlation_id"] or row["outbox_id"],
            causation_id=row["causation_id"],
            payload=_loads_payload(row["payload"]),
        )


def _loads_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return cast("dict[str, Any]", json.loads(raw.decode("utf-8")))
    if isinstance(raw, str):
        return cast("dict[str, Any]", json.loads(raw))
    return {}
