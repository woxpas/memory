"""Timeline events repository.

The append-only fact stream that powers surfacing intelligence. Every reference
writes here; the surfacer reads here to compute decay/momentum/co-occurrence
without scanning chunks/mentions.

Hot path: bulk insert from the pipeline + ad-hoc record() from retrieval.
Reads are out of scope for ingestion (surfacing pipeline owns those).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg


class TimelineRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def record(
        self,
        tenant_id: UUID,
        event_type: str,
        *,
        occurred_at: datetime | None = None,
        valid_time: datetime | None = None,
        doc_id: UUID | None = None,
        chunk_id: UUID | None = None,
        span_id: UUID | None = None,
        block_id: UUID | None = None,
        entity_id: UUID | None = None,
        idea_id: UUID | None = None,
        task_id: UUID | None = None,
        scope_id: UUID | None = None,
        conflict_id: UUID | None = None,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        row = await self._conn.fetchrow(
            """
            INSERT INTO timeline_events (
              tenant_id, occurred_at, valid_time, event_type,
              doc_id, chunk_id, span_id, block_id, entity_id, idea_id, task_id, scope_id, conflict_id,
              actor, payload
            )
            VALUES (
              $1, COALESCE($2, now()), $3, $4,
              $5, $6, $7, $8, $9, $10, $11, $12, $13,
              $14, COALESCE($15::jsonb, '{}'::jsonb)
            )
            RETURNING event_id
            """,
            tenant_id, occurred_at, valid_time, event_type,
            doc_id, chunk_id, span_id, block_id,
            entity_id, idea_id, task_id, scope_id, conflict_id,
            actor, payload,
        )
        return cast("int", row["event_id"])

    async def bulk_record(self, events: list[dict[str, Any]]) -> int:
        """Each dict has the same keys as record() arguments."""
        if not events:
            return 0
        rows = [
            (
                e["tenant_id"],
                e.get("occurred_at"),
                e.get("valid_time"),
                e["event_type"],
                e.get("doc_id"), e.get("chunk_id"), e.get("span_id"), e.get("block_id"),
                e.get("entity_id"), e.get("idea_id"), e.get("task_id"),
                e.get("scope_id"), e.get("conflict_id"),
                e.get("actor"),
                e.get("payload") or {},
            )
            for e in events
        ]
        await self._conn.executemany(
            """
            INSERT INTO timeline_events (
              tenant_id, occurred_at, valid_time, event_type,
              doc_id, chunk_id, span_id, block_id,
              entity_id, idea_id, task_id, scope_id, conflict_id,
              actor, payload
            ) VALUES (
              $1, COALESCE($2, now()), $3, $4,
              $5, $6, $7, $8,
              $9, $10, $11, $12, $13,
              $14, $15::jsonb
            )
            """,
            rows,
        )
        return len(rows)
