"""Predicates repository — read-only access to the canonical vocabulary,
plus the relations.predicate_canonical update that the linking stage uses.

The predicates table is seeded via migration 013 with 88 system-global rows
(tenant_id IS NULL). Per-tenant predicates are an extension point; for v0.2
of memory product we may let tenants register additional canonical predicates
by writing rows with their tenant_id.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


class PredicateRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def list_visible(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """All predicates this tenant can see: system-global (tenant_id IS NULL)
        + this tenant's own rows. Linking embeds the union once at startup.
        identity_relevant included so phase 2.2.3 can filter for accretion."""
        rows = await self._conn.fetch(
            """
            SELECT predicate_id, canonical, kind, aliases, description,
                   identity_relevant
              FROM predicates
             WHERE tenant_id IS NULL OR tenant_id = $1
             ORDER BY kind, canonical
            """,
            tenant_id,
        )
        return [dict(r) for r in rows]

    async def set_relation_canonical(
        self,
        tenant_id: UUID, relation_id: UUID,
        *,
        predicate_canonical: str,
    ) -> None:
        """Flip a relation from 'open' to 'typed' linking-state and stamp
        predicate_canonical. Only updates rows that are still 'open' so that
        a re-linking pass is a safe no-op for already-linked relations."""
        await self._conn.execute(
            """
            UPDATE relations
               SET predicate_canonical = $3,
                   predicate_kind      = 'typed'
             WHERE tenant_id = $1
               AND relation_id = $2
               AND predicate_kind = 'open'
            """,
            tenant_id, relation_id, predicate_canonical,
        )
