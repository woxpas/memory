"""Scopes repository.

Scopes are the multi-membership filter dimension. The agent's scope-suggest /
scope-dedup behavior reads `description`, `embedding_ref`, and `member_centroid_ref`;
the actual semantic match is delegated to QdrantClient.search_scopes().
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import asyncpg

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "scope"


class ScopeRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def create(
        self,
        tenant_id: UUID,
        name: str,
        *,
        description: str | None = None,
        slug: str | None = None,
        parent_scope_id: UUID | None = None,
        color: str | None = None,
        icon: str | None = None,
    ) -> UUID:
        row = await self._conn.fetchrow(
            """
            INSERT INTO scopes (
              tenant_id, name, slug, description, parent_scope_id, color, icon
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (tenant_id, slug) DO UPDATE
              SET name = EXCLUDED.name,
                  description = COALESCE(EXCLUDED.description, scopes.description),
                  updated_at = now()
            RETURNING scope_id
            """,
            tenant_id, name, slug or _slugify(name), description,
            parent_scope_id, color, icon,
        )
        return row["scope_id"]

    async def by_slug(self, tenant_id: UUID, slug: str) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM scopes WHERE tenant_id = $1 AND slug = $2",
            tenant_id, slug,
        )
        return dict(row) if row else None

    async def by_id(self, tenant_id: UUID, scope_id: UUID) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM scopes WHERE tenant_id = $1 AND scope_id = $2",
            tenant_id, scope_id,
        )
        return dict(row) if row else None

    async def list(self, tenant_id: UUID, include_archived: bool = False) -> list[dict[str, Any]]:
        if include_archived:
            rows = await self._conn.fetch(
                "SELECT * FROM scopes WHERE tenant_id = $1 ORDER BY pinned DESC, sort_order, name",
                tenant_id,
            )
        else:
            rows = await self._conn.fetch(
                "SELECT * FROM scopes WHERE tenant_id = $1 AND archived_at IS NULL "
                "ORDER BY pinned DESC, sort_order, name",
                tenant_id,
            )
        return [dict(r) for r in rows]

    async def fuzzy_match(
        self, tenant_id: UUID, query: str, limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Trigram-fuzzy match on name + description. Used by scope-suggest as a
        cheap pre-filter before the Qdrant semantic step."""
        rows = await self._conn.fetch(
            """
            SELECT scope_id, name, slug, description,
                   similarity(name, $2) +
                   COALESCE(similarity(description, $2), 0) AS score
              FROM scopes
             WHERE tenant_id = $1 AND archived_at IS NULL
               AND (name % $2 OR description % $2)
             ORDER BY score DESC
             LIMIT $3
            """,
            tenant_id, query, limit,
        )
        return [dict(r) for r in rows]

    async def update_embedding_ref(
        self, tenant_id: UUID, scope_id: UUID, embedding_ref: UUID,
    ) -> None:
        await self._conn.execute(
            "UPDATE scopes SET embedding_ref = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND scope_id = $2",
            tenant_id, scope_id, embedding_ref,
        )

    async def update_centroid(
        self, tenant_id: UUID, scope_id: UUID, centroid_ref: UUID,
    ) -> None:
        await self._conn.execute(
            "UPDATE scopes "
            "   SET member_centroid_ref = $3, last_centroid_update = now(), updated_at = now() "
            " WHERE tenant_id = $1 AND scope_id = $2",
            tenant_id, scope_id, centroid_ref,
        )

    async def member_count_delta(
        self, tenant_id: UUID, scope_id: UUID, delta: int,
    ) -> None:
        await self._conn.execute(
            "UPDATE scopes "
            "   SET member_count = GREATEST(0, member_count + $3), updated_at = now() "
            " WHERE tenant_id = $1 AND scope_id = $2",
            tenant_id, scope_id, delta,
        )


class DocScopeRepo:
    """doc_scopes junction. assignment_by tracks who/what put the doc in the scope."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def assign(
        self,
        tenant_id: UUID, doc_id: UUID, scope_id: UUID,
        assigned_by: str,
        auto_assigned: bool = False,
        confidence: float | None = None,
    ) -> bool:
        """Returns True if a new assignment was made (False if already present)."""
        row = await self._conn.fetchrow(
            """
            INSERT INTO doc_scopes (doc_id, scope_id, tenant_id,
                                    assigned_by, auto_assigned, confidence)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (doc_id, scope_id) DO NOTHING
            RETURNING 1 AS inserted
            """,
            doc_id, scope_id, tenant_id, assigned_by, auto_assigned, confidence,
        )
        return row is not None

    async def unassign(self, tenant_id: UUID, doc_id: UUID, scope_id: UUID) -> None:
        await self._conn.execute(
            "DELETE FROM doc_scopes "
            " WHERE tenant_id = $1 AND doc_id = $2 AND scope_id = $3",
            tenant_id, doc_id, scope_id,
        )

    async def scopes_for_doc(self, tenant_id: UUID, doc_id: UUID) -> list[UUID]:
        rows = await self._conn.fetch(
            "SELECT scope_id FROM doc_scopes WHERE tenant_id = $1 AND doc_id = $2",
            tenant_id, doc_id,
        )
        return [r["scope_id"] for r in rows]

    async def docs_for_scope(
        self, tenant_id: UUID, scope_id: UUID, limit: int = 50, offset: int = 0,
    ) -> list[UUID]:
        rows = await self._conn.fetch(
            """
            SELECT doc_id FROM doc_scopes
             WHERE tenant_id = $1 AND scope_id = $2
             ORDER BY assigned_at DESC LIMIT $3 OFFSET $4
            """,
            tenant_id, scope_id, limit, offset,
        )
        return [r["doc_id"] for r in rows]
