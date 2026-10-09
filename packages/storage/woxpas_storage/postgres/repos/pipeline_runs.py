"""Pipeline runs repository.

Per-stage execution log. Used for observability + retry orchestration.
Always called within a transaction with the rest of the stage's writes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


class PipelineRunRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def start(
        self,
        tenant_id: UUID, doc_id: UUID, stage: str, pipeline_version: str,
        attempt: int = 1,
    ) -> UUID:
        row = await self._conn.fetchrow(
            """
            INSERT INTO pipeline_runs (
              tenant_id, doc_id, stage, status, pipeline_version, attempt
            ) VALUES ($1, $2, $3, 'running', $4, $5)
            RETURNING run_id
            """,
            tenant_id, doc_id, stage, pipeline_version, attempt,
        )
        return row["run_id"]

    async def succeed(
        self, run_id: UUID, metrics: dict[str, Any] | None = None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE pipeline_runs
               SET status = 'succeeded', finished_at = now(),
                   metrics = COALESCE($2::jsonb, metrics)
             WHERE run_id = $1
            """,
            run_id, metrics,
        )

    async def fail(
        self,
        run_id: UUID,
        error_class: str,
        error_message: str,
        retriable: bool = True,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE pipeline_runs
               SET status = 'failed', finished_at = now(),
                   error_class = $2, error_message = $3, error_retriable = $4,
                   metrics = COALESCE($5::jsonb, metrics)
             WHERE run_id = $1
            """,
            run_id, error_class, error_message, retriable, metrics,
        )

    async def skip(self, run_id: UUID, reason: str) -> None:
        await self._conn.execute(
            """
            UPDATE pipeline_runs
               SET status = 'skipped', finished_at = now(),
                   metrics = jsonb_build_object('skip_reason', $2::text)
             WHERE run_id = $1
            """,
            run_id, reason,
        )

    async def latest_for_stage(
        self, tenant_id: UUID, doc_id: UUID, stage: str,
    ) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            """
            SELECT * FROM pipeline_runs
             WHERE tenant_id = $1 AND doc_id = $2 AND stage = $3
             ORDER BY started_at DESC LIMIT 1
            """,
            tenant_id, doc_id, stage,
        )
        return dict(row) if row else None
