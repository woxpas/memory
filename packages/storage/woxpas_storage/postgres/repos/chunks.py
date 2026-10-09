"""Chunks + spans repositories.

Chunks are the retrieval unit (vector lives in Qdrant; metadata + text + tsvector
live here). Spans are sentence-level granularity for UI highlighting; not indexed
in Qdrant. Both are content-derived IDs → idempotent ON CONFLICT DO NOTHING.

Bulk insert is the hot path. asyncpg.copy_records_to_table is fastest for
large batches (>100 rows); executemany handles the typical case.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from woxpas_core import Chunk, Span


class ChunkRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def bulk_insert(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        rows = [
            (
                c.chunk_id, c.doc_id, c.tenant_id, c.ordinal, c.text, c.token_count,
                c.block_ids, c.source_locator,
                c.qdrant_point_id, c.embed_model, c.embed_model_version,
                c.scope_ids, c.processing_profile,
                c.valid_time_start, c.valid_time_end,
                c.pipeline_version,
            )
            for c in chunks
        ]
        await self._conn.executemany(
            """
            INSERT INTO chunks (
              chunk_id, doc_id, tenant_id, ordinal, text, token_count,
              block_ids, source_locator,
              qdrant_point_id, embed_model, embed_model_version,
              scope_ids, processing_profile,
              valid_time_start, valid_time_end,
              pipeline_version
            ) VALUES (
              $1, $2, $3, $4, $5, $6,
              $7, $8::jsonb,
              $9, $10, $11,
              $12, $13,
              $14, $15,
              $16
            )
            ON CONFLICT (chunk_id) DO NOTHING
            """,
            rows,
        )
        return len(rows)

    async def by_doc(self, tenant_id: UUID, doc_id: UUID) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            """
            SELECT chunk_id, ordinal, text, token_count, block_ids, source_locator,
                   qdrant_point_id, embed_model, embed_model_version,
                   scope_ids, processing_profile,
                   valid_time_start, valid_time_end
              FROM chunks
             WHERE tenant_id = $1 AND doc_id = $2
             ORDER BY ordinal
            """,
            tenant_id, doc_id,
        )
        return [dict(r) for r in rows]

    async def update_scopes(
        self, tenant_id: UUID, doc_id: UUID, scope_ids: list[UUID],
    ) -> None:
        """When a doc's scopes change, mirror to all its chunks for filter pushdown."""
        await self._conn.execute(
            "UPDATE chunks SET scope_ids = $3 "
            "WHERE tenant_id = $1 AND doc_id = $2",
            tenant_id, doc_id, scope_ids,
        )


class SpanRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def bulk_insert(self, tenant_id: UUID, spans: list[Span]) -> int:
        if not spans:
            return 0
        rows = [
            (
                s.span_id, s.chunk_id, s.doc_id, tenant_id, s.block_id,
                s.ordinal, s.text,
                s.offset_in_chunk[0], s.offset_in_chunk[1],
                s.source_locator.model_dump(mode="json"),
                s.pipeline_version,
            )
            for s in spans
        ]
        await self._conn.executemany(
            """
            INSERT INTO spans (
              span_id, chunk_id, doc_id, tenant_id, block_id,
              ordinal, text, offset_in_chunk, source_locator, pipeline_version
            ) VALUES (
              $1, $2, $3, $4, $5,
              $6, $7, int4range($8, $9, '[)'), $10::jsonb, $11
            )
            ON CONFLICT (span_id) DO NOTHING
            """,
            rows,
        )
        return len(rows)

    async def by_chunk(self, tenant_id: UUID, chunk_id: UUID) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            """
            SELECT span_id, ordinal, text,
                   lower(offset_in_chunk) AS offset_start,
                   upper(offset_in_chunk) AS offset_end,
                   source_locator, block_id
              FROM spans
             WHERE tenant_id = $1 AND chunk_id = $2
             ORDER BY ordinal
            """,
            tenant_id, chunk_id,
        )
        return [dict(r) for r in rows]
