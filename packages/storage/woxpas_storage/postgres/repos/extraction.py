"""Repos for extraction-pipeline rows: entities, ideas, tasks, relations.

Minimal surface for Phase 2.1 — insert + simple lookup-or-create. The Phase
2.2 linking stage adds embedding-based entity merge / predicate normalization
via separate methods on these same repos.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


class EntityRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def get_or_create_by_name(
        self,
        tenant_id: UUID,
        *,
        canonical_name: str,
        entity_type: str,
        first_mentioned_at: datetime | None = None,
    ) -> UUID:
        """Lookup by exact (tenant, canonical_name, type). Insert if missing.

        Phase 2.2 linking stage replaces exact-match dedup with embedding
        cosine. For Phase 2.1, exact-match keeps things simple and works
        for the obvious cases ('Ada Lovelace' twice in one doc → one entity).
        """
        existing = await self._conn.fetchval(
            """
            SELECT entity_id FROM entities
             WHERE tenant_id = $1
               AND lower(canonical_name) = lower($2)
               AND type = $3
               AND merged_into_entity_id IS NULL
             LIMIT 1
            """,
            tenant_id, canonical_name, entity_type,
        )
        if existing is not None:
            return existing
        row = await self._conn.fetchrow(
            """
            INSERT INTO entities (
              tenant_id, canonical_name, type, profile,
              first_mentioned_at, last_mentioned_at, mention_count
            ) VALUES (
              $1, $2, $3, '{}'::jsonb,
              COALESCE($4, now()), COALESCE($4, now()), 1
            )
            RETURNING entity_id
            """,
            tenant_id, canonical_name, entity_type, first_mentioned_at,
        )
        return row["entity_id"]

    async def bump_mention(
        self,
        tenant_id: UUID, entity_id: UUID,
        *,
        when: datetime | None = None,
    ) -> None:
        """Increment mention_count + update last_mentioned_at. Cheap update
        used by the extraction stage every time it links an entity to a doc."""
        await self._conn.execute(
            """
            UPDATE entities
               SET mention_count    = mention_count + 1,
                   last_mentioned_at = COALESCE($3, now()),
                   updated_at        = now()
             WHERE tenant_id = $1 AND entity_id = $2
            """,
            tenant_id, entity_id, when,
        )

    async def candidates_for_doc(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return entities first-touched by this doc — for the linking stage to
        scope disambiguation to fresh entities (not the whole tenant).

        Two sources unioned:
          1. Entities referenced by this doc's relations/ideas/tasks (FK joins).
          2. Entities first_mentioned_at >= the doc's extract_facts run window
             on this same doc — covers the case where extraction created an
             entity but the LLM produced no claims/ideas/tasks for it.

        Excludes already-merged entities. Idempotent on re-run.
        """
        rows = await self._conn.fetch(
            """
            WITH ef_run AS (
              SELECT started_at,
                     COALESCE(finished_at, now()) AS ended_at
                FROM pipeline_runs
               WHERE tenant_id = $1 AND doc_id = $2 AND stage = 'extract_facts'
               ORDER BY started_at DESC LIMIT 1
            ),
            referenced AS (
              SELECT subject_entity_id AS eid FROM relations
                WHERE tenant_id = $1 AND doc_id = $2
              UNION
              SELECT object_entity_id  AS eid FROM relations
                WHERE tenant_id = $1 AND doc_id = $2 AND object_entity_id IS NOT NULL
              UNION
              SELECT owner_entity_id   AS eid FROM ideas
                WHERE tenant_id = $1 AND doc_id = $2 AND owner_entity_id IS NOT NULL
              UNION
              SELECT unnest(related_entity_ids) AS eid FROM ideas
                WHERE tenant_id = $1 AND doc_id = $2
              UNION
              SELECT owner_entity_id   AS eid FROM tasks_deadlines
                WHERE tenant_id = $1 AND doc_id = $2 AND owner_entity_id IS NOT NULL
              UNION
              SELECT unnest(related_entity_ids) AS eid FROM tasks_deadlines
                WHERE tenant_id = $1 AND doc_id = $2
            ),
            fresh_in_window AS (
              -- Fallback: entities CREATED OR UPDATED during this doc's
              -- extract_facts run (catches LLM-named entities that didn't
              -- make it into a claim, AND existing entities that were
              -- mention-bumped this run but not yet disambiguated). Uses
              -- updated_at since bump_mention touches it on every reference.
              SELECT entity_id AS eid
                FROM entities, ef_run
               WHERE entities.tenant_id = $1
                 AND entities.updated_at >= ef_run.started_at
                 AND entities.updated_at <= ef_run.ended_at
            ),
            per_doc AS (
              SELECT eid FROM referenced
              UNION
              SELECT eid FROM fresh_in_window
            )
            SELECT e.entity_id, e.canonical_name, e.type, e.mention_count,
                   e.first_mentioned_at, e.created_at
              FROM entities e
              JOIN per_doc p ON p.eid = e.entity_id
             WHERE e.tenant_id = $1
               AND e.merged_into_entity_id IS NULL
            """,
            tenant_id, doc_id,
        )
        return [dict(r) for r in rows]

    async def candidates_same_type(
        self, tenant_id: UUID, entity_id: UUID, entity_type: str,
    ) -> list[dict[str, Any]]:
        """Return non-merged entities of the same type in the tenant, excluding
        the given entity. Used as the search space for disambiguation cosine."""
        rows = await self._conn.fetch(
            """
            SELECT entity_id, canonical_name, type, mention_count,
                   first_mentioned_at, created_at
              FROM entities
             WHERE tenant_id = $1
               AND type = $3
               AND merged_into_entity_id IS NULL
               AND entity_id <> $2
             ORDER BY first_mentioned_at NULLS LAST, created_at
            """,
            tenant_id, entity_id, entity_type,
        )
        return [dict(r) for r in rows]

    async def merge_into(
        self, tenant_id: UUID, *,
        from_entity_id: UUID, into_entity_id: UUID,
    ) -> None:
        """Soft-merge from_entity into into_entity. Sets merged_into_entity_id
        on the duplicate and adds its mention_count to the survivor. FKs on
        relations/ideas/tasks are left in place for provenance — retrieval
        follows the merge chain via COALESCE(merged_into_entity_id, entity_id).
        """
        await self._conn.execute(
            """
            WITH dup AS (
              SELECT mention_count, last_mentioned_at, first_mentioned_at
                FROM entities
               WHERE tenant_id = $1 AND entity_id = $2
            )
            UPDATE entities canonical
               SET mention_count     = canonical.mention_count + dup.mention_count,
                   last_mentioned_at = GREATEST(canonical.last_mentioned_at,
                                                dup.last_mentioned_at),
                   first_mentioned_at = LEAST(canonical.first_mentioned_at,
                                              dup.first_mentioned_at),
                   updated_at         = now()
              FROM dup
             WHERE canonical.tenant_id = $1
               AND canonical.entity_id = $3
            """,
            tenant_id, from_entity_id, into_entity_id,
        )
        await self._conn.execute(
            """
            UPDATE entities
               SET merged_into_entity_id = $3,
                   updated_at            = now()
             WHERE tenant_id = $1 AND entity_id = $2
            """,
            tenant_id, from_entity_id, into_entity_id,
        )


class IdeaRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def insert(
        self,
        tenant_id: UUID,
        *,
        doc_id:           UUID,
        title:            str | None,
        summary:          str,
        category:         str | None = None,
        priority:         str | None = None,
        owner_entity_id:  UUID | None = None,
        related_entity_ids: list[UUID] | None = None,
        confidence:       float | None = None,
        extractor:        str = "extract_facts",
        extractor_version: str = "0.1.0",
        originated_at:    datetime | None = None,
    ) -> UUID:
        # status='raw' is the seed-state for extracted ideas (valid CHECK
        # values: raw|developing|active|dormant|abandoned|realized). The
        # surfacing/agent loop promotes raw → developing → active.
        row = await self._conn.fetchrow(
            """
            INSERT INTO ideas (
              tenant_id, doc_id, title, summary,
              status, category, priority,
              owner_entity_id, related_entity_ids,
              confidence, extractor, extractor_version,
              originated_at
            ) VALUES (
              $1, $2, $3, $4,
              'raw', $5, $6,
              $7, COALESCE($8, ARRAY[]::uuid[]),
              $9, $10, $11,
              COALESCE($12, now())
            )
            RETURNING idea_id
            """,
            tenant_id, doc_id, title, summary,
            category, priority,
            owner_entity_id, related_entity_ids,
            confidence, extractor, extractor_version,
            originated_at,
        )
        return row["idea_id"]


class TaskRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def insert(
        self,
        tenant_id: UUID,
        *,
        doc_id:          UUID,
        title:           str,
        description:     str | None = None,
        kind:            str = "task",       # task | deadline | event | reminder
        priority:        str | None = None,
        due_at:          datetime | None = None,
        all_day:         bool = False,
        owner_entity_id: UUID | None = None,
        related_entity_ids: list[UUID] | None = None,
        confidence:      float | None = None,
        extractor:       str = "extract_facts",
        extractor_version: str = "0.1.0",
        created_by:      str = "extraction",
    ) -> UUID:
        # status='open' + sync_state='local_only' (CHECK enums:
        # status ∈ open|scheduled|in_progress|blocked|done|cancelled|snoozed,
        # sync_state ∈ local_only|clean|dirty|conflicted)
        row = await self._conn.fetchrow(
            """
            INSERT INTO tasks_deadlines (
              tenant_id, doc_id, title, description,
              kind, status, priority, due_at, all_day,
              external_refs, sync_state,
              owner_entity_id, related_entity_ids,
              confidence, extractor, extractor_version, created_by
            ) VALUES (
              $1, $2, $3, $4,
              $5, 'open', $6, $7, $8,
              '[]'::jsonb, 'local_only',
              $9, COALESCE($10, ARRAY[]::uuid[]),
              $11, $12, $13, $14
            )
            RETURNING task_id
            """,
            tenant_id, doc_id, title, description,
            kind, priority, due_at, all_day,
            owner_entity_id, related_entity_ids,
            confidence, extractor, extractor_version, created_by,
        )
        return row["task_id"]


class RelationRepo:
    """Claims: subject-predicate-object triples with raw claim_text.

    Two notions of "kind" — easy to confuse, both live here:
      - relations.predicate_kind ∈ open|typed|functional — the LINKING STATE.
        'open' means the predicate hasn't been normalized to the predicates
        registry yet. 'typed' means it has (Phase 2.2 linking stage flips this).
      - The semantic predicate kind (attribute|relation|temporal|...) lives on
        the predicates registry row pointed to by predicate_canonical.

    Phase 2.1 writes:
      predicate         = raw verb phrase from LLM
      predicate_kind    = 'open' (linking hasn't run yet)
      claim_text        = surface sentence (verbatim)
      predicate_canonical = NULL  (linking populates)

    The LLM's own semantic-kind hint is captured at the stage layer (in run
    metrics + structlog) but not persisted — linking re-classifies via the
    predicates registry which has authoritative `kind` per predicate.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def insert(
        self,
        tenant_id: UUID,
        *,
        subject_entity_id: UUID,
        predicate:         str,                    # raw verb phrase
        claim_text:        str,                    # surface sentence
        object_entity_id:  UUID | None = None,
        object_value:      dict[str, Any] | None = None,
        doc_id:            UUID | None = None,
        confidence:        float | None = None,
        extractor:         str = "extract_facts",
        extractor_version: str = "0.1.0",
    ) -> UUID:
        if object_entity_id is None and object_value is None:
            raise ValueError("relation requires either object_entity_id or object_value")
        row = await self._conn.fetchrow(
            """
            INSERT INTO relations (
              tenant_id, subject_entity_id, predicate, predicate_kind,
              object_entity_id, object_value, doc_id,
              confidence, extractor, extractor_version, claim_text
            ) VALUES (
              $1, $2, $3, 'open',
              $4, $5::jsonb, $6,
              $7, $8, $9, $10
            )
            RETURNING relation_id
            """,
            tenant_id, subject_entity_id, predicate,
            object_entity_id, object_value, doc_id,
            confidence, extractor, extractor_version, claim_text,
        )
        return row["relation_id"]
