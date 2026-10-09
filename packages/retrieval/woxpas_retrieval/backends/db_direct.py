"""DB-direct backend — pure SQL retrieval, no LLM, no vectors.

Covers four intent routes:
  1. TIMELINE     — recently-touched documents/ideas/tasks within a time window
  2. TYPE_LISTING — what tasks need completion / what ideas am I working on /
                    what files in my vault / what scopes do I have
  3. FILE_LOOKUP  — single doc by id or filename pattern
  4. ENTITY_CARD  — entity skeleton (counts) or full (paginated detail)

All methods are async + take a PostgresPool. Sensitivity, scope, archive
filters are applied uniformly. Returns shape primitives (lists of ResultItem,
EntityCard, DocumentDetail) that the orchestrator wraps in RetrievalResult.

Cursor pagination is keyset-based on (created_at DESC, id DESC) for stability
under concurrent inserts. Cursor encoding lives in woxpas_retrieval.cursors.

This backend is the FAST path — every query should complete in <100ms even
with thousands of rows. No retrieval-time analysis; just well-indexed SQL.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from woxpas_retrieval.cursors import decode_cursor, encode_cursor
from woxpas_retrieval.time_resolve import resolve_time_filter
from woxpas_retrieval.types import (
    DocumentDetail,
    EntityCard,
    Filters,
    ResultItem,
    SensitivityFilter,
    TimeFilter,
)

log = structlog.get_logger(__name__)


# Hard cap so a malicious or buggy caller can't request 10M rows.
_HARD_LIMIT = 200


_DOC_REF_PREFIX     = re.compile(r"^doc:([0-9a-f-]{36})$", re.IGNORECASE)
_ENTITY_REF_PREFIX  = re.compile(r"^entity:([0-9a-f-]{36})$", re.IGNORECASE)


def is_doc_ref(query: str) -> UUID | None:
    """Recognize 'doc:<uuid>' shortcut for direct file lookup."""
    m = _DOC_REF_PREFIX.match(query.strip())
    if m:
        try:
            return UUID(m.group(1))
        except ValueError:
            return None
    return None


def is_entity_ref(query: str) -> UUID | None:
    """Recognize 'entity:<uuid>' shortcut for direct entity card."""
    m = _ENTITY_REF_PREFIX.match(query.strip())
    if m:
        try:
            return UUID(m.group(1))
        except ValueError:
            return None
    return None


class DbDirectBackend:
    """Pure SQL backend. Caller injects a PostgresPool; all queries scope by
    tenant_id (RLS enforced via tenant_session)."""

    def __init__(self, pool) -> None:                    # type: ignore[no-untyped-def]
        self._pool = pool

    # ─── 1. TIMELINE ────────────────────────────────────────────────────────

    async def timeline(
        self,
        *,
        tenant_id: UUID,
        filters:   Filters,
        limit:     int,
        cursor:    str | None,
    ) -> tuple[list[ResultItem], str | None, bool]:
        """Documents created/last-mentioned within filters.time, ranked
        chronologically (most recent first). Cursor-paginated.

        Returns (items, next_cursor, truncated).
        """
        since, until = resolve_time_filter(filters.time)
        cstate = decode_cursor(cursor) or {}
        cap = min(limit, _HARD_LIMIT)

        # Phase 5a.5.0 Group B: exclude archived docs from retrieval.
        # `archived_at IS NULL` is the user-visible "live" filter; tenants
        # who explicitly want to see archived docs use a separate path
        # (e.g. /documents/{id}/full direct fetch) that opts in.
        clauses = [
            "d.tenant_id = $1",
            "d.deleted_at IS NULL",
            "d.archived_at IS NULL",
        ]
        params: list[Any] = [tenant_id]

        if since is not None:
            params.append(since)
            clauses.append(f"d.created_at >= ${len(params)}")
        if until is not None:
            params.append(until)
            clauses.append(f"d.created_at < ${len(params)}")

        clauses.extend(_apply_sensitivity_filter(filters.sensitivity, "d", params))
        clauses.extend(_apply_profile_filter(filters.processing_profiles, "d", params))
        clauses.extend(_apply_scope_filter(filters.scope_ids, filters.scope_slugs, "d", params))

        # Keyset pagination: (created_at, doc_id) is the unique sort tuple.
        if cstate:
            try:
                ts = datetime.fromisoformat(cstate["last_sort_value"])
                last_id = UUID(cstate["last_id"])
                params.extend([ts, last_id])
                clauses.append(
                    f"(d.created_at, d.doc_id) < (${len(params)-1}, ${len(params)})"
                )
            except (KeyError, ValueError):
                pass

        params.append(cap + 1)        # +1 to detect truncation
        sql = f"""
            SELECT d.doc_id, d.tenant_id, d.title, d.filename, d.summary,
                   d.processing_profile, d.sensitivity_class,
                   d.created_at, d.last_accessed_at,
                   d.topics_inferred
              FROM documents d
             WHERE {' AND '.join(clauses)}
             ORDER BY d.created_at DESC, d.doc_id DESC
             LIMIT ${len(params)}
        """
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(sql, *params)

        return _shape_doc_items(rows, cap)

    # ─── 2. TYPE LISTINGS ───────────────────────────────────────────────────

    async def list_ideas(
        self,
        *,
        tenant_id: UUID, filters: Filters,
        limit: int, cursor: str | None,
        statuses: list[str] | None = None,
    ) -> tuple[list[ResultItem], str | None, bool]:
        """List ideas, default to the open-ish statuses (raw|developing|active)."""
        return await self._list_typed(
            tenant_id=tenant_id, filters=filters,
            limit=limit, cursor=cursor,
            table="ideas",
            id_col="idea_id",
            kind="idea",
            time_col="originated_at",
            title_col="title",
            summary_col="summary",
            extra_cols=["status", "category", "priority"],
            status_col="status",
            statuses=statuses or ["raw", "developing", "active"],
        )

    async def list_tasks(
        self,
        *,
        tenant_id: UUID, filters: Filters,
        limit: int, cursor: str | None,
        statuses: list[str] | None = None,
    ) -> tuple[list[ResultItem], str | None, bool]:
        """List tasks/deadlines, default to open|scheduled|in_progress|blocked."""
        return await self._list_typed(
            tenant_id=tenant_id, filters=filters,
            limit=limit, cursor=cursor,
            table="tasks_deadlines",
            id_col="task_id",
            kind="task",
            time_col="created_at",
            title_col="title",
            summary_col="description",
            extra_cols=["kind", "status", "priority", "due_at", "all_day"],
            status_col="status",
            statuses=statuses or ["open", "scheduled", "in_progress", "blocked"],
        )

    async def list_files(
        self,
        *,
        tenant_id: UUID, filters: Filters,
        limit: int, cursor: str | None,
    ) -> tuple[list[ResultItem], str | None, bool]:
        """List documents (anything in the vault). Same as timeline but with
        no time filter required."""
        return await self.timeline(
            tenant_id=tenant_id, filters=filters,
            limit=limit, cursor=cursor,
        )

    async def list_scopes(
        self,
        *,
        tenant_id: UUID, filters: Filters,
        limit: int,
    ) -> list[ResultItem]:
        """List the tenant's scopes. Tiny set — no pagination."""
        cap = min(limit, _HARD_LIMIT)
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT scope_id, name, slug, description, member_count,
                       created_at, updated_at
                  FROM scopes
                 WHERE tenant_id = $1 AND archived_at IS NULL
                 ORDER BY name
                 LIMIT $2
                """,
                tenant_id, cap,
            )
        items: list[ResultItem] = []
        for r in rows:
            items.append(ResultItem(
                kind="scope",
                id=r["scope_id"],
                title=r["name"],
                summary=r["description"],
                metadata={
                    "slug": r["slug"],
                    "member_count": r["member_count"],
                },
                created_at=r["created_at"],
            ))
        return items

    # ─── 3. FILE LOOKUP ─────────────────────────────────────────────────────

    async def chunk_with_context(
        self,
        *,
        tenant_id: UUID,
        chunk_id:  UUID,
        context_window: int = 1,
    ) -> dict | None:
        """Tenant-scoped fetch for one chunk plus its prev/next neighbors.

        Used by GET /chunks/<chunk_id> to give agents enough surrounding
        text to "elaborate" on a citation without making multiple calls.
        Returns None if the chunk doesn't exist or belongs to a different
        tenant (the SQL filter is the security boundary; cross-tenant
        callers see "not found" indistinguishably from "doesn't exist").

        Shape: {target: chunk_dict, prev: chunk_dict | None,
                next: chunk_dict | None, doc: doc_dict}
        """
        if context_window < 0 or context_window > 5:
            # Defensive — endpoint should validate, but bound here too.
            context_window = 1

        async with self._pool.tenant_session(tenant_id) as conn:
            target = await conn.fetchrow(
                """
                SELECT c.chunk_id, c.doc_id, c.ordinal, c.text, c.token_count,
                       d.title, d.filename, d.topics_inferred,
                       d.processing_profile, d.sensitivity_class,
                       d.created_at, d.last_accessed_at
                  FROM chunks c
                  JOIN documents d ON d.tenant_id = c.tenant_id
                                  AND d.doc_id    = c.doc_id
                 WHERE c.tenant_id = $1
                   AND c.chunk_id  = $2
                   AND d.deleted_at IS NULL
                """,
                tenant_id, chunk_id,
            )
            if target is None:
                return None

            target_ordinal = int(target["ordinal"])
            doc_id = target["doc_id"]
            target_ords = list(range(
                max(0, target_ordinal - context_window),
                target_ordinal + context_window + 1,
            ))
            target_ords.remove(target_ordinal)
            neighbors = []
            if target_ords:
                neighbors = await conn.fetch(
                    """
                    SELECT chunk_id, ordinal, text, token_count
                      FROM chunks
                     WHERE tenant_id = $1
                       AND doc_id    = $2
                       AND ordinal   = ANY ($3::int[])
                    """,
                    tenant_id, doc_id, target_ords,
                )

        prev_chunks: list[dict] = []
        next_chunks: list[dict] = []
        for n in neighbors:
            row = {
                "chunk_id":    n["chunk_id"],
                "ordinal":     int(n["ordinal"]),
                "text":        n["text"] or "",
                "token_count": n["token_count"],
            }
            if row["ordinal"] < target_ordinal:
                prev_chunks.append(row)
            else:
                next_chunks.append(row)
        prev_chunks.sort(key=lambda r: r["ordinal"])
        next_chunks.sort(key=lambda r: r["ordinal"])

        return {
            "target": {
                "chunk_id":    target["chunk_id"],
                "ordinal":     target_ordinal,
                "text":        target["text"] or "",
                "token_count": target["token_count"],
            },
            "prev": prev_chunks,
            "next": next_chunks,
            "doc": {
                "doc_id":             doc_id,
                "title":              target["title"],
                "filename":           target["filename"],
                "topics":             list(target["topics_inferred"] or []),
                "processing_profile": target["processing_profile"],
                "sensitivity_class":  target["sensitivity_class"] or "standard",
                "created_at":         target["created_at"],
                "last_accessed_at":   target["last_accessed_at"],
            },
        }


    async def chunks_by_ids(
        self,
        *,
        tenant_id: UUID,
        chunk_ids: list[UUID],
    ) -> list[ResultItem]:
        """Tenant-scoped lookup for a list of chunk ids.

        Used by synthesis follow-up turns where the agent passes back
        chunk_ids from a prior answer (`pinned_chunk_ids`). Returns chunk
        ResultItems with their parent doc's title + topics in metadata so
        the citation envelope can attribute correctly. Chunks not owned
        by this tenant are silently dropped (security boundary)."""
        if not chunk_ids:
            return []
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT c.chunk_id, c.doc_id, c.ordinal, c.text, c.token_count,
                       d.title, d.filename, d.topics_inferred,
                       d.processing_profile, d.sensitivity_class,
                       d.created_at, d.last_accessed_at
                  FROM chunks c
                  JOIN documents d ON d.tenant_id = c.tenant_id
                                  AND d.doc_id    = c.doc_id
                 WHERE c.tenant_id = $1
                   AND c.chunk_id  = ANY ($2::uuid[])
                   AND d.deleted_at IS NULL
                """,
                tenant_id, chunk_ids,
            )
        out: list[ResultItem] = []
        for r in rows:
            out.append(ResultItem(
                kind="chunk",
                id=r["chunk_id"],
                title=r["title"],
                # Use 280 to match the vector backend's snippet length so
                # downstream consumers see a consistent shape.
                snippet=(r["text"] or "")[:280],
                metadata={
                    "doc_id":             str(r["doc_id"]),
                    "ordinal":            r["ordinal"],
                    "filename":           r["filename"],
                    "processing_profile": r["processing_profile"],
                    "topics":             list(r["topics_inferred"] or []),
                    "full_text":          r["text"] or "",   # for synthesis pinning
                },
                sensitivity_class=r["sensitivity_class"] or "standard",
                created_at=r["created_at"],
                last_accessed_at=r["last_accessed_at"],
            ))
        return out


    async def file_by_id(
        self,
        *,
        tenant_id: UUID, doc_id: UUID,
        chunks_preview: int = 5,
    ) -> DocumentDetail | None:
        """Single doc by id, with up to N chunk previews."""
        async with self._pool.tenant_session(tenant_id) as conn:
            doc = await conn.fetchrow(
                """
                SELECT doc_id, title, filename, summary, topics_inferred,
                       processing_profile, sensitivity_class,
                       canonical_uri, raw_uri
                  FROM documents
                 WHERE tenant_id = $1 AND doc_id = $2 AND deleted_at IS NULL
                """,
                tenant_id, doc_id,
            )
            if doc is None:
                return None

            chunk_rows = await conn.fetch(
                """
                SELECT chunk_id, ordinal, text, token_count
                  FROM chunks
                 WHERE tenant_id = $1 AND doc_id = $2
                 ORDER BY ordinal
                 LIMIT $3
                """,
                tenant_id, doc_id, max(0, chunks_preview),
            )

        chunks_preview_items: list[ResultItem] = []
        for cr in chunk_rows:
            chunks_preview_items.append(ResultItem(
                kind="chunk",
                id=cr["chunk_id"],
                snippet=(cr["text"] or "")[:280],
                metadata={"ordinal": cr["ordinal"], "token_count": cr["token_count"]},
                sensitivity_class=doc["sensitivity_class"] or "standard",
            ))

        return DocumentDetail(
            doc_id=doc["doc_id"],
            title=doc["title"],
            filename=doc["filename"],
            processing_profile=doc["processing_profile"],
            sensitivity_class=doc["sensitivity_class"] or "standard",
            summary=doc["summary"],
            topics=list(doc["topics_inferred"] or []),
            chunks_preview=chunks_preview_items,
            canonical_uri=doc["canonical_uri"],
            raw_uri=doc["raw_uri"],
        )

    async def file_by_name(
        self,
        *,
        tenant_id: UUID, name_pattern: str,
        limit: int = 10,
    ) -> list[ResultItem]:
        """Filename ILIKE match. Returns ranked list of candidates the agent
        can disambiguate or follow up with file_by_id for full content."""
        cap = min(limit, _HARD_LIMIT)
        like_pat = f"%{name_pattern.strip()}%"
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT doc_id, title, filename, summary,
                       processing_profile, sensitivity_class,
                       created_at, last_accessed_at
                  FROM documents
                 WHERE tenant_id = $1
                   AND deleted_at IS NULL
                   AND (filename ILIKE $2 OR title ILIKE $2)
                 ORDER BY created_at DESC
                 LIMIT $3
                """,
                tenant_id, like_pat, cap,
            )
        items, _, _ = _shape_doc_items(rows, cap)
        return items

    # ─── 4. ENTITY CARD ─────────────────────────────────────────────────────

    async def entity_skeleton(
        self,
        *,
        tenant_id: UUID, entity_id: UUID,
    ) -> EntityCard | None:
        """Counts + first/last mentioned + a small recent slice. Cheap (~5 SQL)."""
        async with self._pool.tenant_session(tenant_id) as conn:
            ent = await conn.fetchrow(
                """
                SELECT entity_id, canonical_name, type,
                       mention_count, first_mentioned_at, last_mentioned_at
                  FROM entities
                 WHERE tenant_id = $1
                   AND COALESCE(merged_into_entity_id, entity_id) = $2
                 LIMIT 1
                """,
                tenant_id, entity_id,
            )
            if ent is None:
                return None

            counts = dict(await conn.fetchrow(
                """
                SELECT
                  (SELECT COUNT(*) FROM relations
                    WHERE tenant_id = $1
                      AND (subject_entity_id = $2 OR object_entity_id = $2)
                      AND archived_at IS NULL)                AS relations,
                  (SELECT COUNT(*) FROM ideas
                    WHERE tenant_id = $1
                      AND (owner_entity_id = $2
                           OR $2 = ANY (related_entity_ids)))  AS ideas,
                  (SELECT COUNT(*) FROM tasks_deadlines
                    WHERE tenant_id = $1
                      AND (owner_entity_id = $2
                           OR $2 = ANY (related_entity_ids))
                      AND status NOT IN ('done','cancelled'))  AS open_tasks,
                  (SELECT COUNT(*) FROM entity_profile_facts
                    WHERE tenant_id = $1 AND entity_id = $2
                      AND superseded_by IS NULL)               AS profile_facts,
                  (SELECT COUNT(*) FROM conflicts
                    WHERE tenant_id = $1
                      AND $2 = ANY (subject_entity_ids)
                      AND resolved_state = 'open')             AS open_conflicts
                """,
                tenant_id, entity_id,
            ))

            recent_relations = await conn.fetch(
                """
                SELECT relation_id, predicate, predicate_canonical,
                       claim_text, doc_id, created_at
                  FROM relations
                 WHERE tenant_id = $1
                   AND (subject_entity_id = $2 OR object_entity_id = $2)
                   AND archived_at IS NULL
                 ORDER BY created_at DESC
                 LIMIT 5
                """,
                tenant_id, entity_id,
            )

            # Top profile facts for the skeleton — gives the agent durable
            # identity context ("Ada lives in Berlin, prefers async") without
            # forcing a follow-up to entity_full for the common "tell me about"
            # query. Capped at 10 for the skeleton scope; full lists everything.
            top_facts = await conn.fetch(
                """
                SELECT fact_category, fact_kind, fact_value,
                       confidence, is_user_asserted, updated_at
                  FROM entity_profile_facts
                 WHERE tenant_id = $1 AND entity_id = $2
                   AND superseded_by IS NULL
                 ORDER BY is_user_asserted DESC, confidence DESC, updated_at DESC
                 LIMIT 10
                """,
                tenant_id, entity_id,
            )

        relations_items = [
            ResultItem(
                kind="claim",
                id=r["relation_id"],
                snippet=(r["claim_text"] or r["predicate"])[:280],
                metadata={
                    "predicate":           r["predicate"],
                    "predicate_canonical": r["predicate_canonical"],
                },
                created_at=r["created_at"],
            )
            for r in recent_relations
        ]

        return EntityCard(
            entity_id=ent["entity_id"],
            canonical_name=ent["canonical_name"],
            type=ent["type"],
            mention_count=ent["mention_count"] or 0,
            first_mentioned_at=ent["first_mentioned_at"],
            last_mentioned_at=ent["last_mentioned_at"],
            counts=dict(counts),
            recent_relations=relations_items,
            profile_facts=[
                {
                    "category":         f["fact_category"],
                    "kind":             f["fact_kind"],
                    "value":            f["fact_value"],
                    "confidence":       float(f["confidence"]),
                    "is_user_asserted": f["is_user_asserted"],
                    "updated_at":       f["updated_at"].isoformat(),
                }
                for f in top_facts
            ],
        )

    async def entity_full(
        self,
        *,
        tenant_id: UUID, entity_id: UUID,
        page_limit: int = 20,
        time:       TimeFilter | None = None,
    ) -> EntityCard | None:
        """Skeleton + paginated recent ideas/tasks/mentions + all profile facts.

        `time` filter narrows recent_ideas / recent_tasks / recent_relations to
        items within (since, until). Profile facts ARE NOT filtered — they're
        durable identity claims, not time-bound activity. So "everything about
        Ada in the last 7 days" returns all profile facts (the identity layer
        is current as of now) PLUS time-windowed recent activity.
        """
        card = await self.entity_skeleton(tenant_id=tenant_id, entity_id=entity_id)
        if card is None:
            return None
        page = min(page_limit, _HARD_LIMIT)
        since, until = resolve_time_filter(time)

        # Build the WHERE-clauses for time filtering. Each table uses a
        # different time column:
        #   ideas              → originated_at
        #   tasks_deadlines    → created_at  (and the existing ORDER preserves due_at)
        #   relations          → created_at
        ideas_clauses:    list[str] = []
        ideas_params:     list[Any] = [tenant_id, entity_id]
        if since is not None:
            ideas_params.append(since)
            ideas_clauses.append(f"originated_at >= ${len(ideas_params)}")
        if until is not None:
            ideas_params.append(until)
            ideas_clauses.append(f"originated_at < ${len(ideas_params)}")
        ideas_params.append(page)

        tasks_clauses:   list[str] = []
        tasks_params:    list[Any] = [tenant_id, entity_id]
        if since is not None:
            tasks_params.append(since)
            tasks_clauses.append(f"created_at >= ${len(tasks_params)}")
        if until is not None:
            tasks_params.append(until)
            tasks_clauses.append(f"created_at < ${len(tasks_params)}")
        tasks_params.append(page)

        rel_clauses:     list[str] = []
        rel_params:      list[Any] = [tenant_id, entity_id]
        if since is not None:
            rel_params.append(since)
            rel_clauses.append(f"created_at >= ${len(rel_params)}")
        if until is not None:
            rel_params.append(until)
            rel_clauses.append(f"created_at < ${len(rel_params)}")
        rel_params.append(page)

        ideas_where = " AND " + " AND ".join(ideas_clauses) if ideas_clauses else ""
        tasks_where = " AND " + " AND ".join(tasks_clauses) if tasks_clauses else ""
        rel_where   = " AND " + " AND ".join(rel_clauses)   if rel_clauses   else ""

        async with self._pool.tenant_session(tenant_id) as conn:
            ideas = await conn.fetch(
                f"""
                SELECT idea_id, title, summary, status, category, originated_at
                  FROM ideas
                 WHERE tenant_id = $1
                   AND (owner_entity_id = $2 OR $2 = ANY (related_entity_ids))
                   {ideas_where}
                 ORDER BY originated_at DESC
                 LIMIT ${len(ideas_params)}
                """,
                *ideas_params,
            )
            tasks = await conn.fetch(
                f"""
                SELECT task_id, title, description, kind, status, priority,
                       due_at, all_day, created_at
                  FROM tasks_deadlines
                 WHERE tenant_id = $1
                   AND (owner_entity_id = $2 OR $2 = ANY (related_entity_ids))
                   {tasks_where}
                 ORDER BY due_at NULLS LAST, created_at DESC
                 LIMIT ${len(tasks_params)}
                """,
                *tasks_params,
            )
            # Time-filtered recent_relations REPLACES the skeleton's top-5 list
            # when caller provides a time window.
            if since is not None or until is not None:
                rels = await conn.fetch(
                    f"""
                    SELECT relation_id, predicate, predicate_canonical,
                           claim_text, doc_id, created_at
                      FROM relations
                     WHERE tenant_id = $1
                       AND (subject_entity_id = $2 OR object_entity_id = $2)
                       AND archived_at IS NULL
                       {rel_where}
                     ORDER BY created_at DESC
                     LIMIT ${len(rel_params)}
                    """,
                    *rel_params,
                )
                card.recent_relations = [
                    ResultItem(
                        kind="claim",
                        id=r["relation_id"],
                        snippet=(r["claim_text"] or r["predicate"])[:280],
                        metadata={
                            "predicate":           r["predicate"],
                            "predicate_canonical": r["predicate_canonical"],
                        },
                        created_at=r["created_at"],
                    )
                    for r in rels
                ]
            facts = await conn.fetch(
                """
                SELECT fact_category, fact_kind, fact_value, confidence,
                       is_user_asserted, created_at, updated_at
                  FROM entity_profile_facts
                 WHERE tenant_id = $1 AND entity_id = $2
                   AND superseded_by IS NULL
                 ORDER BY fact_category, fact_kind
                """,
                tenant_id, entity_id,
            )

        card.recent_ideas = [
            ResultItem(
                kind="idea", id=r["idea_id"], title=r["title"],
                summary=r["summary"],
                metadata={"status": r["status"], "category": r["category"]},
                created_at=r["originated_at"],
            )
            for r in ideas
        ]
        card.recent_tasks = [
            ResultItem(
                kind="task", id=r["task_id"], title=r["title"],
                summary=r["description"],
                metadata={
                    "kind": r["kind"], "status": r["status"],
                    "priority": r["priority"],
                    "due_at": r["due_at"].isoformat() if r["due_at"] else None,
                    "all_day": r["all_day"],
                },
                created_at=r["created_at"],
            )
            for r in tasks
        ]
        card.profile_facts = [
            {
                "category":         r["fact_category"],
                "kind":             r["fact_kind"],
                "value":            r["fact_value"],
                "confidence":       float(r["confidence"]),
                "is_user_asserted": r["is_user_asserted"],
                "updated_at":       r["updated_at"].isoformat(),
            }
            for r in facts
        ]
        return card

    async def entity_by_name(
        self,
        *,
        tenant_id: UUID, name_pattern: str,
        limit: int = 10,
    ) -> list[ResultItem]:
        """Fuzzy entity name match — returns ResultItem(kind=entity) so the
        agent can disambiguate before requesting the full card."""
        cap = min(limit, _HARD_LIMIT)
        like_pat = f"%{name_pattern.strip()}%"
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT entity_id, canonical_name, type, mention_count,
                       last_mentioned_at, first_mentioned_at
                  FROM entities
                 WHERE tenant_id = $1
                   AND merged_into_entity_id IS NULL
                   AND canonical_name ILIKE $2
                 ORDER BY mention_count DESC, canonical_name
                 LIMIT $3
                """,
                tenant_id, like_pat, cap,
            )
        return [
            ResultItem(
                kind="entity",
                id=r["entity_id"],
                title=r["canonical_name"],
                metadata={
                    "type": r["type"],
                    "mention_count": r["mention_count"],
                },
                last_accessed_at=r["last_mentioned_at"],
            )
            for r in rows
        ]

    # ─── shared helper for type listings ────────────────────────────────────

    async def _list_typed(
        self,
        *,
        tenant_id: UUID, filters: Filters,
        limit: int, cursor: str | None,
        table: str, id_col: str, kind: str,
        time_col: str, title_col: str, summary_col: str,
        extra_cols: list[str],
        status_col: str | None,
        statuses: list[str],
    ) -> tuple[list[ResultItem], str | None, bool]:
        """Generic type listing: ideas / tasks / etc.

        Filters that apply across listings:
          - filters.time     (since/until on time_col)
          - filters.entity_ids (matches owner_entity_id OR related_entity_ids)
        Sensitivity filter is applied via JOIN on documents (the row's source
        doc) so 'exclude_sensitive' filters out ideas/tasks attached to
        sensitive docs.
        """
        since, until = resolve_time_filter(filters.time)
        cstate = decode_cursor(cursor) or {}
        cap = min(limit, _HARD_LIMIT)

        clauses = ["t.tenant_id = $1"]
        params: list[Any] = [tenant_id]

        if status_col is not None and statuses:
            params.append(statuses)
            clauses.append(f"t.{status_col} = ANY (${len(params)}::text[])")
        if since is not None:
            params.append(since)
            clauses.append(f"t.{time_col} >= ${len(params)}")
        if until is not None:
            params.append(until)
            clauses.append(f"t.{time_col} < ${len(params)}")

        # Entity filter
        if filters.entity_ids:
            params.append(filters.entity_ids)
            clauses.append(
                f"(t.owner_entity_id = ANY (${len(params)}::uuid[]) "
                f"OR t.related_entity_ids && ${len(params)}::uuid[])"
            )

        # Sensitivity filter via JOIN — only when needed.
        select_extra = ", ".join(f"t.{c}" for c in extra_cols)
        sens_join = ""
        if filters.sensitivity != SensitivityFilter.ANY:
            sens_join = " JOIN documents d ON d.doc_id = t.doc_id AND d.tenant_id = t.tenant_id"
            clauses.extend(_apply_sensitivity_filter(filters.sensitivity, "d", params))

        # Keyset pagination on (time_col, id_col).
        if cstate:
            try:
                ts = datetime.fromisoformat(cstate["last_sort_value"])
                last_id = UUID(cstate["last_id"])
                params.extend([ts, last_id])
                clauses.append(
                    f"(t.{time_col}, t.{id_col}) < (${len(params)-1}, ${len(params)})"
                )
            except (KeyError, ValueError):
                pass

        params.append(cap + 1)
        sql = f"""
            SELECT t.{id_col} AS id, t.{title_col} AS title,
                   t.{summary_col} AS summary, t.{time_col} AS sort_time,
                   t.doc_id, {select_extra}
              FROM {table} t{sens_join}
             WHERE {' AND '.join(clauses)}
             ORDER BY t.{time_col} DESC, t.{id_col} DESC
             LIMIT ${len(params)}
        """
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(sql, *params)

        truncated = len(rows) > cap
        rows = rows[:cap]

        items: list[ResultItem] = []
        for r in rows:
            metadata = {c: r[c] for c in extra_cols}
            # JSON-friendly normalization for datetime-like fields.
            for k, v in list(metadata.items()):
                if isinstance(v, datetime):
                    metadata[k] = v.isoformat()
            items.append(ResultItem(
                kind=kind,
                id=r["id"],
                title=r["title"],
                summary=r["summary"],
                metadata=metadata,
                created_at=r["sort_time"],
            ))

        next_cursor: str | None = None
        if truncated and items:
            last = rows[-1]
            next_cursor = encode_cursor({
                "last_sort_value": last["sort_time"].isoformat(),
                "last_id":         str(last["id"]),
            })
        return items, next_cursor, truncated


# ─── Module-level helpers ───────────────────────────────────────────────────

def _apply_sensitivity_filter(
    s: SensitivityFilter, alias: str, params: list[Any],
) -> list[str]:
    if s == SensitivityFilter.ANY:
        return []
    if s == SensitivityFilter.EXCLUDE_SENSITIVE:
        return [f"{alias}.sensitivity_class = 'standard'"]
    if s == SensitivityFilter.ONLY_SENSITIVE:
        return [f"{alias}.sensitivity_class = 'sensitive'"]
    return []


def _apply_profile_filter(
    profiles: list[str], alias: str, params: list[Any],
) -> list[str]:
    if not profiles:
        return []
    params.append(profiles)
    return [f"{alias}.processing_profile = ANY (${len(params)}::text[])"]


def _apply_scope_filter(
    scope_ids: list[UUID], scope_slugs: list[str], alias: str, params: list[Any],
) -> list[str]:
    if not scope_ids and not scope_slugs:
        return []
    if scope_ids and scope_slugs:
        params.append(scope_ids)
        params.append(scope_slugs)
        return [
            f"EXISTS (SELECT 1 FROM doc_scopes ds JOIN scopes s ON s.scope_id=ds.scope_id "
            f"WHERE ds.tenant_id = {alias}.tenant_id AND ds.doc_id = {alias}.doc_id "
            f"AND (ds.scope_id = ANY (${len(params)-1}::uuid[]) "
            f"OR s.slug = ANY (${len(params)}::text[])))"
        ]
    if scope_ids:
        params.append(scope_ids)
        return [
            f"EXISTS (SELECT 1 FROM doc_scopes ds "
            f"WHERE ds.tenant_id = {alias}.tenant_id AND ds.doc_id = {alias}.doc_id "
            f"AND ds.scope_id = ANY (${len(params)}::uuid[]))"
        ]
    params.append(scope_slugs)
    return [
        f"EXISTS (SELECT 1 FROM doc_scopes ds JOIN scopes s ON s.scope_id=ds.scope_id "
        f"WHERE ds.tenant_id = {alias}.tenant_id AND ds.doc_id = {alias}.doc_id "
        f"AND s.slug = ANY (${len(params)}::text[]))"
    ]


def _shape_doc_items(
    rows, cap: int,                              # type: ignore[no-untyped-def]
) -> tuple[list[ResultItem], str | None, bool]:
    truncated = len(rows) > cap
    rows = rows[:cap]
    items: list[ResultItem] = []
    for r in rows:
        # asyncpg.Record supports `in` + indexed access; coerce optional fields
        # via a plain dict view for column-presence checks.
        d = dict(r)
        items.append(ResultItem(
            kind="doc",
            id=d["doc_id"],
            title=d.get("title"),
            summary=d.get("summary"),
            snippet=None,
            metadata={
                "filename":           d.get("filename"),
                "processing_profile": d["processing_profile"],
                "topics":             list(d.get("topics_inferred") or []),
            },
            sensitivity_class=d.get("sensitivity_class") or "standard",
            created_at=d.get("created_at"),
            last_accessed_at=d.get("last_accessed_at"),
        ))
    next_cursor: str | None = None
    if truncated and items:
        last = dict(rows[-1])
        next_cursor = encode_cursor({
            "last_sort_value": last["created_at"].isoformat(),
            "last_id":         str(last["doc_id"]),
        })
    return items, next_cursor, truncated
