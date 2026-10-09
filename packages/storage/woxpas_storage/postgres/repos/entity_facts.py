"""entity_profile_facts repository — durable identity facts about entities.

This is the layer that powers identity_card(). Phase 2.2.3 linking-stage
profile-fact accretion writes here when a self-entity claim's predicate
is identity_relevant (per migration 017).

Bitemporal: system_time + valid_time both supported. For Phase 2.2.3 v1 we
only use system_time (when the fact was recorded) because the LLM's
extracted claims rarely carry valid_time hints — Phase 2.3 conflict
detection adds valid_time when supersession is detected.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


class EntityProfileFactRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def upsert_fact(
        self,
        tenant_id: UUID,
        *,
        entity_id:        UUID,
        fact_category:    str,
        fact_kind:        str,
        fact_value:       dict[str, Any],
        confidence:       float = 0.7,
        is_user_asserted: bool = False,
        extractor:        str = "link",
        extractor_version: str = "0.1.0",
    ) -> tuple[UUID, bool]:
        """Idempotent insert keyed on (entity, fact_kind, fact_value).

        If the same (entity_id, fact_kind, fact_value) already exists we just
        bump updated_at + take the higher confidence — Phase 2.3 conflict
        detection handles the harder case where fact_value DIFFERS for the
        same (entity, fact_kind).

        Returns (fact_id, was_inserted). was_inserted=False means the row
        already existed and we just touched its timestamps.
        """
        existing = await self._conn.fetchrow(
            """
            SELECT fact_id, confidence
              FROM entity_profile_facts
             WHERE tenant_id = $1
               AND entity_id = $2
               AND fact_kind = $3
               AND fact_value = $4::jsonb
               AND superseded_by IS NULL
             LIMIT 1
            """,
            tenant_id, entity_id, fact_kind, fact_value,
        )
        if existing is not None:
            new_conf = max(float(existing["confidence"]), confidence)
            await self._conn.execute(
                """
                UPDATE entity_profile_facts
                   SET confidence = $3,
                       updated_at = now()
                 WHERE tenant_id = $1 AND fact_id = $2
                """,
                tenant_id, existing["fact_id"], new_conf,
            )
            return existing["fact_id"], False

        row = await self._conn.fetchrow(
            """
            INSERT INTO entity_profile_facts (
              tenant_id, entity_id,
              fact_category, fact_kind, fact_value,
              confidence, is_user_asserted,
              extractor, extractor_version
            ) VALUES (
              $1, $2,
              $3, $4, $5::jsonb,
              $6, $7,
              $8, $9
            )
            RETURNING fact_id
            """,
            tenant_id, entity_id,
            fact_category, fact_kind, fact_value,
            confidence, is_user_asserted,
            extractor, extractor_version,
        )
        return row["fact_id"], True
