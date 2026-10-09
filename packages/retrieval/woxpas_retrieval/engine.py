"""RetrievalEngine — single entry point for the retrieval pipeline.

  recall(request) → RetrievalResult

Flow:
  1. Classify intent (rules, with LLM fallback for low-confidence)
  2. Merge extracted_filters into request.filters (compound queries)
  3. Resolve entity-name hints to entity_ids (used by ENTITY_CARD/GRAPH)
  4. Dispatch to the right backend
  5. Rerank if SEMANTIC and request.rerank=True
  6. Enrich items with conflict provenance (Phase 2.3 wired through)
  7. Bump access_count + last_accessed_at on retrieved rows (memory layer)
  8. Build truncation hint per intent
  9. Wrap in RetrievalResult with metrics + intent_used + uncertainty flag

Design:
  - Engine is a class with backend instances injected once. No global state.
  - Each method is async + stateless. Two concurrent recall() calls don't
    interfere (DB pool handles connection sharing).
  - Errors at backend layer caught + returned as empty results with hint.
    Pillar-2 (accuracy): never return wrong data; surface "couldn't run that"
    cleanly so the agent can retry.
  - Latency tracked per-stage, surfaced in `metrics`.

Cloud-portability: the engine's contract is the pure types in types.py.
HTTP layer (Phase 3.9) is a thin adapter over `recall()`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from woxpas_retrieval.backends import (
    DbDirectBackend,
    GraphBackend,
    VectorBackend,
)
from woxpas_retrieval.backends.db_direct import is_doc_ref, is_entity_ref
from woxpas_retrieval.enrichment import (
    attach_conflict_provenance,
    build_truncation_hint,
    bump_mentions,
)
from woxpas_retrieval.intent import ExtractedFilters, classify
from woxpas_retrieval.types import (
    CardMode,
    Filters,
    GraphView,
    Hint,
    Intent,
    ResultItem,
    ResultShape,
    RetrievalRequest,
    RetrievalResult,
    TimeFilter,
)

if TYPE_CHECKING:
    from woxpas_runtime import EmbedderBackend, LlmBackend, RerankerBackend
    from woxpas_storage import PostgresPool, QdrantClient

log = structlog.get_logger(__name__)


# Phase 4.9 — entity enrichment caps. Held at module-scope so they can be
# tuned in v0.2 without touching the engine logic.
_MAX_ENRICH_ENTITIES   = 3   # max distinct entities to enrich per query
_MAX_CHUNKS_PER_ENTITY = 8   # cap on chunks pulled per entity


def _format_fact_value(fact_kind: str, fact_value: Any) -> str:
    """Render a profile-fact value as a short readable string for the
    dossier. fact_value is jsonb — could be {kind: 'literal', value: 'X'},
    a plain string, a dict with named subfields, etc."""
    if fact_value is None:
        return ""
    if isinstance(fact_value, str):
        return f"{fact_kind}: {fact_value[:80]}"
    if isinstance(fact_value, dict):
        # Common shapes from extraction: {value: 'X'} or {value: 'X', unit: 'Y'}
        v = fact_value.get("value")
        if isinstance(v, str):
            unit = fact_value.get("unit")
            return f"{fact_kind}: {v[:60]}" + (f" {unit}" if unit else "")
        # Fallback: serialize compactly
        return f"{fact_kind}: {str(fact_value)[:80]}"
    return f"{fact_kind}: {str(fact_value)[:80]}"


class RetrievalEngine:
    """Orchestrator for the retrieval pipeline.

    Construct once at server startup; share across all recall() calls.
    Backend instances are injected — easy to swap embedder/reranker/LLM
    without touching the engine.
    """

    def __init__(
        self,
        *,
        pool:            PostgresPool,
        qdrant:          QdrantClient,
        embedder:        EmbedderBackend,
        reranker:        RerankerBackend | None = None,
        intent_llm:      LlmBackend | None      = None,
    ) -> None:
        self._pool      = pool
        self._db_direct = DbDirectBackend(pool)
        self._vector    = VectorBackend(qdrant, pool, embedder)
        self._graph     = GraphBackend(pool)
        self._reranker  = reranker
        self._intent_llm = intent_llm

    # ─── public entrypoint ──────────────────────────────────────────────────

    async def recall(
        self,
        request: RetrievalRequest,
        *,
        tenant_id: UUID,
    ) -> RetrievalResult:
        """Single entry. tenant_id derived from auth at the API layer; never
        from the request body (cloud-portability requirement)."""
        t_start = time.monotonic()
        metrics: dict[str, float | int | str | None] = {}

        # 1. Classify intent (+ extract compound filters).
        t0 = time.monotonic()
        classified = await classify(
            request.query_text,
            intent_hint=request.intent_hint,
            llm=self._intent_llm,
        )
        metrics["intent_classify_ms"] = round((time.monotonic() - t0) * 1000, 1)
        metrics["intent_source"]      = classified.source
        intent = classified.intent

        # 2. Merge extracted filters into request.filters (compound queries).
        merged_filters = self._merge_filters(
            request.filters, classified.extracted_filters,
        )

        # 3. Determine the requested shape (default per intent).
        shape = request.shape or _natural_shape(intent)

        # 4. Dispatch.
        result = await self._dispatch(
            request=request, tenant_id=tenant_id,
            intent=intent, shape=shape,
            filters=merged_filters,
            extracted_entity_names=list(classified.extracted_filters.entity_names),
            metrics=metrics,
        )

        # 5. Enrich + bump (only when items are present — graph/card paths
        # already have their own item-shaped fields, but they don't carry
        # ResultItems requiring conflict enrichment in v1).
        if result.items:
            t0 = time.monotonic()
            await attach_conflict_provenance(
                self._pool, tenant_id=tenant_id, items=result.items,
            )
            metrics["conflict_enrich_ms"] = round((time.monotonic() - t0) * 1000, 1)

            t0 = time.monotonic()
            await bump_mentions(
                self._pool, tenant_id=tenant_id, items=result.items,
            )
            metrics["bump_ms"] = round((time.monotonic() - t0) * 1000, 1)

        # Card / detail / graph also get a bump on the directly-served
        # entity / doc / graph nodes.
        await self._bump_non_item_results(tenant_id, result)

        # 5.5 — Phase 4.9: entity-aware enrichment ("know-you" / an agent feature).
        # Detect named entities in the query, pull rich state, walk back to
        # source chunks, merge into items + build situational dossier. Runs
        # before truncation hint so hint sees the enriched item count.
        if request.enrich_with_entities and classified.extracted_filters.entity_names:
            t0 = time.monotonic()
            entity_chunks, dossier = await self._enrich_with_entity_context(
                tenant_id=tenant_id,
                entity_names=list(classified.extracted_filters.entity_names),
            )
            if entity_chunks:
                # Merge into items, dedup by chunk_id.
                seen = {it.id for it in result.items}
                for ec in entity_chunks:
                    if ec.id not in seen:
                        result.items.append(ec)
                        seen.add(ec.id)
            if dossier:
                result.entity_dossier = dossier
            metrics["entity_enrich_ms"] = round((time.monotonic() - t0) * 1000, 1)
            metrics["entity_enrich_chunks"] = len(entity_chunks)

        # 6. Truncation hint.
        if result.hint is None and result.truncated:
            result.hint = build_truncation_hint(
                items=result.items,
                truncated=result.truncated,
                total_estimated=result.total_estimated,
                intent=intent,
            )

        # 7. Final metadata.
        result.intent_used      = intent
        result.intent_uncertain = classified.uncertain
        metrics["total_ms"]     = round((time.monotonic() - t_start) * 1000, 1)
        result.metrics          = {**result.metrics, **metrics}

        log.info("retrieval.recall.ok",
                 request_id=str(result.request_id),
                 intent=intent.value, shape=shape.value,
                 items=len(result.items),
                 ms=metrics["total_ms"])
        return result

    async def _enrich_with_entity_context(
        self,
        *,
        tenant_id:    UUID,
        entity_names: list[str],
    ) -> tuple[list[ResultItem], str | None]:
        """Phase 4.9 — entity-aware retrieval enrichment ("know-you" feature).

        For each named entity in the query (cap 3), resolves to entity_id
        via fuzzy name match, pulls rich state (profile facts + active
        relations + active tasks + active ideas + open conflicts),
        traces back to source chunks, and builds a situational dossier
        text the synthesis prompt injects as INTERPRETATION context.

        Profile facts contribute to the dossier text but NOT to the chunk
        citation_map (facts are accreted; they don't carry chunk_id).
        Active relations/tasks/ideas DO carry chunk_id and contribute
        chunks (capped 8 per entity, deduplicated).

        Precision: matches by canonical_name ILIKE; takes top result by
        mention_count (de-facto threshold — well-known entities are more
        likely to be the user's intended reference).
        """
        if not entity_names:
            return [], None

        # Cap entities; prioritize earliest-occurring names in the query
        # (proxy for primary subject).
        names_capped = entity_names[: _MAX_ENRICH_ENTITIES]

        all_chunk_ids: list[UUID] = []
        dossier_lines: list[str] = []

        async with self._pool.tenant_session(tenant_id) as conn:
            for raw_name in names_capped:
                # Resolve: ILIKE match, prefer higher mention_count.
                ent_row = await conn.fetchrow(
                    """
                    SELECT entity_id, canonical_name, type, mention_count,
                           first_mentioned_at, last_mentioned_at
                      FROM entities
                     WHERE tenant_id = $1
                       AND merged_into_entity_id IS NULL
                       AND canonical_name ILIKE $2
                     ORDER BY mention_count DESC,
                              char_length(canonical_name) ASC
                     LIMIT 1
                    """,
                    tenant_id, f"%{raw_name.strip()}%",
                )
                if ent_row is None:
                    continue

                entity_id   = ent_row["entity_id"]
                canon_name  = ent_row["canonical_name"]

                # Active relations: NOT archived, ordered by recency.
                relations = await conn.fetch(
                    """
                    SELECT relation_id, predicate, predicate_canonical,
                           claim_text, chunk_id
                      FROM relations
                     WHERE tenant_id = $1
                       AND (subject_entity_id = $2 OR object_entity_id = $2)
                       AND archived_at IS NULL
                       AND chunk_id IS NOT NULL
                     ORDER BY created_at DESC
                     LIMIT 5
                    """,
                    tenant_id, entity_id,
                )

                # Active tasks: not done/cancelled. Surface overdue + due_at.
                tasks = await conn.fetch(
                    """
                    SELECT task_id, title, status, due_at, chunk_id,
                           (due_at IS NOT NULL AND due_at < now()
                            AND status NOT IN ('done','cancelled')) AS is_overdue
                      FROM tasks_deadlines
                     WHERE tenant_id = $1
                       AND (owner_entity_id = $2 OR $2 = ANY (related_entity_ids))
                       AND status NOT IN ('done','cancelled')
                       AND chunk_id IS NOT NULL
                     ORDER BY (due_at IS NOT NULL AND due_at < now()) DESC,
                              due_at ASC NULLS LAST,
                              created_at DESC
                     LIMIT 5
                    """,
                    tenant_id, entity_id,
                )

                # Active ideas: not abandoned/realized.
                ideas = await conn.fetch(
                    """
                    SELECT idea_id, title, summary, status, category, chunk_id
                      FROM ideas
                     WHERE tenant_id = $1
                       AND (owner_entity_id = $2 OR $2 = ANY (related_entity_ids))
                       AND status NOT IN ('abandoned','realized')
                       AND chunk_id IS NOT NULL
                     ORDER BY originated_at DESC
                     LIMIT 5
                    """,
                    tenant_id, entity_id,
                )

                # Top profile facts (for dossier text, not chunk citation).
                facts = await conn.fetch(
                    """
                    SELECT fact_category, fact_kind, fact_value
                      FROM entity_profile_facts
                     WHERE tenant_id = $1 AND entity_id = $2
                       AND superseded_by IS NULL
                     ORDER BY is_user_asserted DESC, confidence DESC, updated_at DESC
                     LIMIT 3
                    """,
                    tenant_id, entity_id,
                )

                # Open conflicts where this entity is implicated.
                conflict_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM conflicts
                     WHERE tenant_id = $1
                       AND $2 = ANY (subject_entity_ids)
                       AND resolved_state = 'open'
                    """,
                    tenant_id, entity_id,
                )

                # Collect chunk_ids for citation_map enrichment.
                for r in relations:
                    if r["chunk_id"]:
                        all_chunk_ids.append(r["chunk_id"])
                for t in tasks:
                    if t["chunk_id"]:
                        all_chunk_ids.append(t["chunk_id"])
                for i in ideas:
                    if i["chunk_id"]:
                        all_chunk_ids.append(i["chunk_id"])

                # Build dossier line for this entity.
                summary_bits: list[str] = []
                # Profile-fact summary (1-line). Each fact_value is jsonb;
                # extract a string representation if value is short.
                fact_strs = [
                    _format_fact_value(f["fact_kind"], f["fact_value"])
                    for f in facts
                ]
                fact_strs = [s for s in fact_strs if s]
                if fact_strs:
                    summary_bits.append(", ".join(fact_strs[:3]))

                # Active relation summary.
                if relations:
                    pred_strs = [
                        (r["predicate_canonical"] or r["predicate"])
                        for r in relations[:3]
                    ]
                    summary_bits.append(
                        f"recent: {', '.join(pred_strs)}"
                    )

                # Task state — emphasize overdue + count.
                overdue = sum(1 for t in tasks if t["is_overdue"])
                if tasks:
                    bit = f"{len(tasks)} active task{'s' if len(tasks) != 1 else ''}"
                    if overdue:
                        bit += f" ({overdue} overdue)"
                    summary_bits.append(bit)

                # Open conflicts.
                if conflict_count:
                    summary_bits.append(
                        f"{conflict_count} open conflict{'s' if conflict_count != 1 else ''}"
                    )

                if summary_bits:
                    dossier_lines.append(
                        f"  {canon_name} — {'; '.join(summary_bits)}."
                    )
                else:
                    dossier_lines.append(
                        f"  {canon_name} — known entity (type: {ent_row['type']}; "
                        f"{ent_row['mention_count']} mentions)."
                    )

        # Resolve all collected chunk_ids → ResultItems via the existing
        # tenant-scoped fetch.
        chunks: list[ResultItem] = []
        if all_chunk_ids:
            # Dedupe preserving order
            seen_chunks: set[UUID] = set()
            ordered: list[UUID] = []
            for cid in all_chunk_ids:
                if cid not in seen_chunks:
                    seen_chunks.add(cid)
                    ordered.append(cid)
            # Cap at 8 chunks per entity = up to 24 total for 3 entities
            ordered = ordered[: _MAX_ENRICH_ENTITIES * _MAX_CHUNKS_PER_ENTITY]
            chunks = await self._db_direct.chunks_by_ids(
                tenant_id=tenant_id, chunk_ids=ordered,
            )

        dossier_text: str | None = None
        if dossier_lines:
            dossier_text = (
                "Context about entities mentioned in this query:\n"
                + "\n".join(dossier_lines)
                + "\n[DO NOT cite this block; cite only the source chunks below.]"
            )

        log.info("retrieval.entity_enrichment",
                 entities=len(names_capped),
                 entities_resolved=len(dossier_lines),
                 chunks_added=len(chunks))

        return chunks, dossier_text


    async def recall_chunk_with_context(
        self,
        chunk_id: UUID,
        *,
        tenant_id:      UUID,
        context_window: int = 1,
    ) -> dict[str, Any] | None:
        """Public wrapper for db_direct.chunk_with_context. Used by the
        GET /chunks/<chunk_id> endpoint to return a chunk with its prev/
        next neighbors. Returns None when not found or cross-tenant —
        callers should map to 404 (don't differentiate, security)."""
        return await self._db_direct.chunk_with_context(
            tenant_id=tenant_id, chunk_id=chunk_id,
            context_window=context_window,
        )


    async def recall_chunks_by_ids(
        self,
        chunk_ids: list[UUID],
        *,
        tenant_id: UUID,
    ) -> list[ResultItem]:
        """Tenant-scoped chunk fetch for follow-up synthesis turns.

        Used by SynthesisEngine when SynthesisRequest.pinned_chunk_ids is
        set: the agent passes chunk_ids from a prior turn, the engine
        re-uses those exact chunks (with stable numeric ids) for the new
        prompt's citation map. Cross-tenant chunk_ids are silently dropped
        — the SQL filters on tenant_id (Pillar 3 / security boundary).
        """
        if not chunk_ids:
            return []
        return await self._db_direct.chunks_by_ids(
            tenant_id=tenant_id, chunk_ids=chunk_ids,
        )

    # ─── dispatch ───────────────────────────────────────────────────────────

    async def _dispatch(
        self,
        *,
        request:                RetrievalRequest,
        tenant_id:              UUID,
        intent:                 Intent,
        shape:                  ResultShape,
        filters:                Filters,
        extracted_entity_names: list[str],
        metrics:                dict[str, Any],
    ) -> RetrievalResult:
        """Route to the right backend and shape the response."""
        # FILE_LOOKUP: if the query is `doc:<uuid>` short-form, go straight.
        if intent == Intent.FILE_LOOKUP:
            return await self._dispatch_file_lookup(
                request, tenant_id, filters, extracted_entity_names,
            )

        if intent == Intent.ENTITY_CARD:
            return await self._dispatch_entity_card(
                request, tenant_id, filters, extracted_entity_names,
            )

        if intent == Intent.TIMELINE:
            return await self._dispatch_timeline(request, tenant_id, filters)

        if intent == Intent.TYPE_LISTING:
            return await self._dispatch_type_listing(
                request, tenant_id, filters,
            )

        if intent == Intent.GRAPH:
            return await self._dispatch_graph(
                request, tenant_id, filters, extracted_entity_names,
            )

        # Default + AUTO that wasn't decided → SEMANTIC
        return await self._dispatch_semantic(
            request, tenant_id, filters, metrics,
        )

    # ─── intent-specific dispatchers ────────────────────────────────────────

    async def _dispatch_file_lookup(
        self,
        request: RetrievalRequest, tenant_id: UUID,
        filters: Filters, extracted_names: list[str],
    ) -> RetrievalResult:
        # 1. Try `doc:<uuid>` short form
        doc_uuid = is_doc_ref(request.query_text)
        if doc_uuid is not None:
            detail = await self._db_direct.file_by_id(
                tenant_id=tenant_id, doc_id=doc_uuid,
            )
            return _wrap_detail(request, detail, intent=Intent.FILE_LOOKUP)

        # 2. Otherwise filename ILIKE — strip cue words from query first
        pattern = _strip_file_cues(request.query_text)
        items = await self._db_direct.file_by_name(
            tenant_id=tenant_id, name_pattern=pattern, limit=request.limit,
        )
        return RetrievalResult(
            request_id=request.request_id,
            intent_used=Intent.FILE_LOOKUP,
            shape=ResultShape.LIST,
            items=items,
        )

    async def _dispatch_entity_card(
        self,
        request: RetrievalRequest, tenant_id: UUID,
        filters: Filters, extracted_names: list[str],
    ) -> RetrievalResult:
        # 1. `entity:<uuid>` short form
        ent_uuid = is_entity_ref(request.query_text)
        if ent_uuid is not None:
            seed_id: UUID | None = ent_uuid
        elif filters.entity_ids:
            seed_id = filters.entity_ids[0]
        elif extracted_names:
            seed_id = await self._resolve_name(tenant_id, extracted_names[0])
        else:
            seed_id = None

        # Self-reference fallback: "me", "myself", "my identity", or a bare
        # name that resolves to nothing → try the tenant's self-entity.
        if seed_id is None:
            seed_id = await self._resolve_self_entity(tenant_id)

        if seed_id is None:
            return RetrievalResult(
                request_id=request.request_id,
                intent_used=Intent.ENTITY_CARD,
                shape=ResultShape.CARD,
                hint=Hint(
                    message="No entity found for this query.",
                    suggested_action=None,
                ),
            )

        # Auto-detect "everything"/"all"/"full" cues — sets card_mode=FULL
        # so the user doesn't have to pass it explicitly. Caller's explicit
        # CardMode.FULL takes priority; this only upgrades from SKELETON.
        effective_mode = request.card_mode
        if effective_mode == CardMode.SKELETON and _wants_full_card(request.query_text):
            effective_mode = CardMode.FULL

        if effective_mode == CardMode.FULL:
            card = await self._db_direct.entity_full(
                tenant_id=tenant_id, entity_id=seed_id,
                page_limit=request.limit,
                time=filters.time,                          # Phase 3.8 fix #1
            )
        else:
            card = await self._db_direct.entity_skeleton(
                tenant_id=tenant_id, entity_id=seed_id,
            )
        return RetrievalResult(
            request_id=request.request_id,
            intent_used=Intent.ENTITY_CARD,
            shape=ResultShape.CARD,
            card=card,
        )

    async def _dispatch_timeline(
        self,
        request: RetrievalRequest, tenant_id: UUID, filters: Filters,
    ) -> RetrievalResult:
        items, cursor, truncated = await self._db_direct.timeline(
            tenant_id=tenant_id, filters=filters,
            limit=request.limit, cursor=request.cursor,
        )
        return RetrievalResult(
            request_id=request.request_id,
            intent_used=Intent.TIMELINE,
            shape=ResultShape.STREAM,
            items=items,
            truncated=truncated,
            next_cursor=cursor,
        )

    async def _dispatch_type_listing(
        self,
        request: RetrievalRequest, tenant_id: UUID, filters: Filters,
    ) -> RetrievalResult:
        # Heuristic: dispatch to the right typed listing based on query
        # keywords. The classifier matched the type-listing pattern but
        # didn't tell us WHICH type — peek at the query.
        q = request.query_text.lower()
        if any(w in q for w in ("idea", "ideas")):
            items, cursor, truncated = await self._db_direct.list_ideas(
                tenant_id=tenant_id, filters=filters,
                limit=request.limit, cursor=request.cursor,
            )
        elif any(w in q for w in ("task", "todo", "to-do", "deadline")):
            items, cursor, truncated = await self._db_direct.list_tasks(
                tenant_id=tenant_id, filters=filters,
                limit=request.limit, cursor=request.cursor,
            )
        elif any(w in q for w in ("scope", "project")):
            items = await self._db_direct.list_scopes(
                tenant_id=tenant_id, filters=filters, limit=request.limit,
            )
            cursor, truncated = None, False
        else:
            # Default: files / docs / artefacts
            items, cursor, truncated = await self._db_direct.list_files(
                tenant_id=tenant_id, filters=filters,
                limit=request.limit, cursor=request.cursor,
            )
        return RetrievalResult(
            request_id=request.request_id,
            intent_used=Intent.TYPE_LISTING,
            shape=ResultShape.LIST,
            items=items,
            truncated=truncated,
            next_cursor=cursor,
        )

    async def _dispatch_graph(
        self,
        request: RetrievalRequest, tenant_id: UUID,
        filters: Filters, extracted_names: list[str],
    ) -> RetrievalResult:
        # Resolve names → entity_ids
        entity_ids: list[UUID] = list(filters.entity_ids)
        for name in extracted_names:
            rid = await self._resolve_name(tenant_id, name)
            if rid is not None and rid not in entity_ids:
                entity_ids.append(rid)

        if not entity_ids:
            return RetrievalResult(
                request_id=request.request_id,
                intent_used=Intent.GRAPH,
                shape=ResultShape.GRAPH,
                hint=Hint(
                    message="No entity reference found in the query.",
                    suggested_action=None,
                ),
            )

        view: GraphView | None
        if len(entity_ids) >= 2:
            view = await self._graph.path_between(
                tenant_id=tenant_id,
                from_entity_id=entity_ids[0],
                to_entity_id=entity_ids[1],
                max_depth=request.graph_depth + 2,    # path queries can go deeper
                predicate_kinds=filters.predicate_kinds or None,
            )
        else:
            view = await self._graph.traverse(
                tenant_id=tenant_id, root_entity_id=entity_ids[0],
                depth=request.graph_depth,
                predicate_kinds=filters.predicate_kinds or None,
                with_paths=True,
            )

        if view is None:
            return RetrievalResult(
                request_id=request.request_id,
                intent_used=Intent.GRAPH,
                shape=ResultShape.GRAPH,
                hint=Hint(
                    message="Couldn't find that entity.",
                    suggested_action=None,
                ),
            )

        hint: Hint | None = None
        if view.truncated_at_level:
            dropped_total = sum(view.truncated_at_level.values())
            hint = Hint(
                message=f"+{dropped_total} more connections at deeper or "
                        f"more-saturated levels. Narrow predicate_kinds or "
                        f"increase per-level limits to see more.",
                suggested_action="graph",
            )

        return RetrievalResult(
            request_id=request.request_id,
            intent_used=Intent.GRAPH,
            shape=ResultShape.GRAPH,
            graph=view,
            hint=hint,
        )

    async def _dispatch_semantic(
        self,
        request:   RetrievalRequest,
        tenant_id: UUID,
        filters:   Filters,
        metrics:   dict[str, Any],
    ) -> RetrievalResult:
        t0 = time.monotonic()
        items, cursor, truncated = await self._vector.search(
            tenant_id=tenant_id,
            query_text=request.query_text,
            filters=filters,
            limit=request.limit,
            cursor=request.cursor,
        )
        metrics["vector_search_ms"] = round((time.monotonic() - t0) * 1000, 1)

        # Optional rerank pass + noise filtering. Vector search returns
        # top-K even for irrelevant queries (nearest by cosine, however far
        # from the query). The reranker score is the right gate: BGE-v2-m3
        # logits in the -10..+10 range; -5 is a permissive "obvious noise"
        # cutoff that keeps weak-but-relevant matches.
        had_results_before_filter = len(items)
        noise_filtered = 0
        if request.rerank and self._reranker is not None and len(items) >= 2:
            t0 = time.monotonic()
            items = await self._rerank_items(
                query=request.query_text, items=items,
            )
            metrics["rerank_ms"] = round((time.monotonic() - t0) * 1000, 1)
            # Drop items below the noise threshold. "score" was set by
            # rerank; if missing, treat as -inf so we don't accidentally
            # filter unscored items.
            kept: list[ResultItem] = []
            for it in items:
                if it.score is None or it.score >= _RERANK_NOISE_THRESHOLD:
                    kept.append(it)
                else:
                    noise_filtered += 1
            items = kept
            metrics["noise_filtered"] = noise_filtered

        # If the noise filter dropped everything, surface a clearer "no
        # relevant results" hint rather than an empty list with no context.
        hint: Hint | None = None
        if not items and had_results_before_filter > 0 and noise_filtered > 0:
            hint = Hint(
                message=f"Found {had_results_before_filter} potential matches "
                        f"but none scored highly enough to be relevant. Try "
                        f"rephrasing your query or use a more specific term.",
                suggested_action="narrow_query",
            )

        return RetrievalResult(
            request_id=request.request_id,
            intent_used=Intent.SEMANTIC,
            shape=ResultShape.LIST,
            items=items,
            truncated=truncated and len(items) > 0,
            next_cursor=cursor if items else None,
            hint=hint,
        )

    # ─── helpers ────────────────────────────────────────────────────────────

    async def _resolve_name(
        self, tenant_id: UUID, name: str,
    ) -> UUID | None:
        """Best-effort name→entity_id resolution. Returns the highest-mention
        entity matching the name (DbDirectBackend.entity_by_name orders by
        mention_count DESC). Returns None if no match."""
        candidates = await self._db_direct.entity_by_name(
            tenant_id=tenant_id, name_pattern=name, limit=1,
        )
        return candidates[0].id if candidates else None

    async def _resolve_self_entity(self, tenant_id: UUID) -> UUID | None:
        """Return the tenant's self-entity_id, or None if not set."""
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT self_entity_id FROM tenants WHERE tenant_id = $1",
                tenant_id,
            )
        return row["self_entity_id"] if row else None

    async def _rerank_items(
        self, *, query: str, items: list[ResultItem],
    ) -> list[ResultItem]:
        """Cross-encoder rerank. Skips items without snippet/title/summary
        text (nothing to score). Stable on ties."""
        scoreable: list[tuple[int, str]] = []
        for i, it in enumerate(items):
            text = it.snippet or it.summary or it.title or ""
            if text:
                scoreable.append((i, text))
        if len(scoreable) < 2:
            return items
        assert self._reranker is not None
        scores = await self._reranker.score(
            query, [t for _, t in scoreable], batch_size=16,
        )
        # Build new score map; preserve original score on items we didn't rerank.
        score_by_idx = {
            scoreable[k][0]: scores[k] for k in range(len(scoreable))
        }
        for i, it in enumerate(items):
            if i in score_by_idx:
                it.score = score_by_idx[i]
        # Stable sort by reranker score DESC; original order preserved on ties.
        return sorted(
            items, key=lambda x: (x.score if x.score is not None else -1e9),
            reverse=True,
        )

    async def _bump_non_item_results(
        self, tenant_id: UUID, result: RetrievalResult,
    ) -> None:
        """Card / detail / graph results don't surface as ResultItems but
        still represent retrievals. Bump the directly-served entities / docs."""
        # Entity card → bump the seed entity.
        if result.card is not None:
            await bump_mentions(
                self._pool, tenant_id=tenant_id,
                items=[ResultItem(kind="entity", id=result.card.entity_id)],
            )
        # File detail → bump the doc.
        if result.detail is not None:
            await bump_mentions(
                self._pool, tenant_id=tenant_id,
                items=[ResultItem(kind="doc", id=result.detail.doc_id)],
            )
        # Graph → bump every entity in the view.
        if result.graph is not None and result.graph.nodes:
            entity_items = [
                ResultItem(kind="entity", id=n.entity_id)
                for n in result.graph.nodes
            ]
            await bump_mentions(
                self._pool, tenant_id=tenant_id, items=entity_items,
            )

    @staticmethod
    def _merge_filters(
        from_request: Filters, from_extracted: ExtractedFilters,
    ) -> Filters:
        """Merge classifier-extracted filters into the caller-supplied ones.
        Caller's explicit filters WIN — the agent gets to override the
        classifier when it knows better."""
        # Time
        time_f: TimeFilter | None = from_request.time
        if time_f is None and from_extracted.time is not None:
            time_f = from_extracted.time

        # Sensitivity / scopes / profiles / predicates / archived flag —
        # caller-set wins; extracted phrases don't currently override these.
        # (scope_phrases are name-based — orchestrator could resolve to slugs
        # later, but for v1 we just expose them via classifier diagnostics.)
        return Filters(
            scope_ids           = from_request.scope_ids,
            scope_slugs         = from_request.scope_slugs,
            time                = time_f,
            sensitivity         = from_request.sensitivity,
            processing_profiles = from_request.processing_profiles,
            entity_ids          = from_request.entity_ids,
            predicate_kinds     = from_request.predicate_kinds,
            include_archived    = from_request.include_archived,
        )


# ─── module-level helpers ───────────────────────────────────────────────────

# Reranker logit threshold below which a result is "obvious noise". For
# BGE-reranker-v2-m3: highly relevant ~+3..+10, weakly relevant -3..+2,
# irrelevant -10..-5. -5 is permissive (keeps weak matches that may still
# be agent-useful) while dropping clearly-unrelated content.
_RERANK_NOISE_THRESHOLD = -5.0


_FULL_CARD_RE = __import__("re").compile(
    r"\b(?:everything|all\s+(?:about|info|details)|full\s+(?:profile|details|card)|"
    r"complete\s+(?:profile|details|info)|everything\s+(?:about|on))\b",
    __import__("re").IGNORECASE,
)


def _wants_full_card(query_text: str) -> bool:
    """Detect language that signals the user wants the full entity profile,
    not the skeleton counts. Caller's explicit card_mode=FULL takes priority;
    this only upgrades from SKELETON when the query language is clear."""
    return bool(_FULL_CARD_RE.search(query_text or ""))


def _natural_shape(intent: Intent) -> ResultShape:
    """Default shape for each intent when the caller doesn't specify one."""
    return {
        Intent.SEMANTIC:      ResultShape.LIST,
        Intent.TIMELINE:      ResultShape.STREAM,
        Intent.TYPE_LISTING:  ResultShape.LIST,
        Intent.FILE_LOOKUP:   ResultShape.DETAIL,
        Intent.ENTITY_CARD:   ResultShape.CARD,
        Intent.GRAPH:         ResultShape.GRAPH,
        Intent.AUTO:          ResultShape.LIST,
    }[intent]


def _strip_file_cues(query: str) -> str:
    """Remove file-lookup cue words to leave a filename pattern.
    'show me the file called notes.md' → 'notes.md'."""
    q = query.strip()
    cues = (
        "show me the file called",
        "show me the file named",
        "show the file called",
        "open the file called",
        "open file called",
        "open file",
        "file called",
        "file named",
        "contents of",
        "show the file",
        "show me",
    )
    lo = q.lower()
    for cue in cues:
        if lo.startswith(cue + " "):
            return q[len(cue) + 1:].strip().strip("'\"`")
    return q


def _wrap_detail(
    request: RetrievalRequest, detail: Any, *, intent: Intent,
) -> RetrievalResult:
    """Helper used by file_lookup to wrap a (possibly-None) DocumentDetail."""
    if detail is None:
        return RetrievalResult(
            request_id=request.request_id, intent_used=intent,
            shape=ResultShape.DETAIL,
            hint=Hint(message="Document not found.", suggested_action=None),
        )
    return RetrievalResult(
        request_id=request.request_id, intent_used=intent,
        shape=ResultShape.DETAIL,
        detail=detail,
    )
