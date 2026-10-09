"""Canonical blocks (CDM) repository.

Bulk-insert path is the hot one — extraction emits dozens of blocks per doc.
Block IDs are content-derived in the caller (Block.make), so re-ingestion is
idempotent: ON CONFLICT DO NOTHING on block_id.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from woxpas_core import Block


class CanonicalBlockRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def bulk_insert(
        self, tenant_id: UUID, doc_id: UUID, pipeline_version: str, blocks: list[Block],
    ) -> int:
        if not blocks:
            return 0
        rows: list[tuple[Any, ...]] = [
            (
                b.block_id, doc_id, tenant_id, b.ordinal, b.type.value,
                b.level, b.parent_block_id, b.text, b.attrs,
                b.source_locator.model_dump(mode="json"),
                pipeline_version,
            )
            for b in blocks
        ]
        await self._conn.executemany(
            """
            INSERT INTO canonical_blocks (
              block_id, doc_id, tenant_id, ordinal, type,
              level, parent_block_id, text, attrs, source_locator, pipeline_version
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11)
            ON CONFLICT (block_id) DO NOTHING
            """,
            rows,
        )
        return len(rows)

    async def by_doc(self, tenant_id: UUID, doc_id: UUID) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            """
            SELECT block_id, ordinal, type, level, parent_block_id,
                   text, attrs, source_locator
              FROM canonical_blocks
             WHERE tenant_id = $1 AND doc_id = $2
             ORDER BY ordinal
            """,
            tenant_id, doc_id,
        )
        return [dict(r) for r in rows]
