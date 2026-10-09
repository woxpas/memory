"""Vector backend — hybrid retrieval over Qdrant chunks + summaries.

Three-head search over chunks (dense + sparse + ColBERT), fused via Reciprocal
Rank Fusion alongside dense summaries. When the embedder doesn't produce sparse
or ColBERT outputs (older single-head ONNX exports), those search legs are
silently skipped — the backend gracefully degrades to dense-only.

Search legs per query:
  1. CHUNKS dense   — semantic passage retrieval (always)
  2. CHUNKS sparse  — lexical / exact-term matching (when embedder produces sparse)
  3. CHUNKS colbert — late-interaction per-token matching (when embedder produces ColBERT)
  4. SUMMARIES dense — doc-level semantic matches (always)

All results fused via RRF (k=60). Top-K passes to the reranker before returning.

Post-filter: sensitivity is on `documents`, not on chunk payloads. We hydrate
from Postgres, join sensitivity + title + archived_at, then drop chunks from
sensitive docs when the filter excludes them.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import structlog
from qdrant_client.http import models as qm

from woxpas_retrieval.cursors import decode_cursor, encode_cursor
from woxpas_retrieval.time_resolve import resolve_time_filter
from woxpas_retrieval.types import (
    Filters,
    Provenance,
    ResultItem,
    SensitivityFilter,
)

log = structlog.get_logger(__name__)


# RRF constant — standard "60" works well across collections and doesn't
# need tuning for typical retrieval depth (≤200 candidates).
_RRF_K = 60

# How many candidates to fetch per search leg BEFORE fusion + rerank.
_CHUNK_DENSE_CANDIDATES   = 50
_CHUNK_SPARSE_CANDIDATES  = 50
_CHUNK_COLBERT_CANDIDATES = 50
_SUMMARY_CANDIDATES       = 25

_HARD_LIMIT = 200


class VectorBackend:
    """Hybrid vector backend. Pulls chunks (dense + sparse + ColBERT) and
    summaries (dense) from Qdrant, fuses via RRF, hydrates from Postgres for
    citations + sensitivity filtering.

    The reranker is wired separately (Phase 3.4) — this backend returns the
    fused candidate list; the orchestrator decides when to rerank.
    """

    def __init__(self, qdrant, pool, embedder) -> None:    # type: ignore[no-untyped-def]
        self._qdrant   = qdrant
        self._pool     = pool
        self._embedder = embedder

    async def search(
        self,
        *,
        tenant_id: UUID,
        query_text: str,
        filters: Filters,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[ResultItem], str | None, bool]:
        """Run hybrid search and return ranked ResultItems.

        Cursor pagination is offset-based here (vector results are an ordered
        list; we slice). Cursor carries `offset` only.
        """
        if not query_text.strip():
            return [], None, False

        cap = min(limit, _HARD_LIMIT)
        cstate = decode_cursor(cursor) or {}
        offset = int(cstate.get("offset", 0))

        # 1. Embed query — three-head when the ONNX export supports it.
        batch = await self._embedder.embed([query_text], batch_size=1)
        if not batch.dense:
            log.warning("vector.search.no_query_embedding")
            return [], None, False
        qvec = batch.dense[0]
        qsparse = batch.sparse[0] if batch.sparse else {}
        qcolbert = batch.colbert[0] if batch.colbert else []

        # 2. Build Qdrant filter from request filters.
        qfilter = _build_qdrant_filter(tenant_id, filters)

        # 3. Search all available heads in parallel.
        ranked_lists: list[list[dict[str, Any]]] = []

        # Chunks — dense (always).
        chunks_dense = await self._search_chunks(
            head="dense", qvec=qvec,
            qfilter=qfilter, limit=offset + cap + _CHUNK_DENSE_CANDIDATES,
        )
        ranked_lists.append(_extract_chunk_hits(chunks_dense))

        # Chunks — sparse (when embedder produces lexical weights).
        if qsparse:
            chunks_sparse = await self._search_chunks_sparse(
                qsparse=qsparse,
                qfilter=qfilter, limit=offset + cap + _CHUNK_SPARSE_CANDIDATES,
            )
            ranked_lists.append(_extract_chunk_hits(chunks_sparse))

        # Chunks — ColBERT (when embedder produces per-token vectors).
        # Mean-pool the query token vectors into a single 1024d vector and
        # search against the colbert named head. Per-token ColBERT vectors
        # are still stored in Qdrant (multi-vector MAX_SIM index), so
        # re-indexing later to use native multi-vector query is a Qdrant
        # upgrade away — no data migration needed.
        if qcolbert:
            colbert_mean = _mean_colbert_query(qcolbert)
            chunks_colbert = await self._search_chunks(
                head="colbert", qvec=colbert_mean,
                qfilter=qfilter, limit=offset + cap + _CHUNK_COLBERT_CANDIDATES,
            )
            ranked_lists.append(_extract_chunk_hits(chunks_colbert))

        # Summaries — dense (always).
        summaries = await self._search_summaries(
            qvec=qvec,
            qfilter=qfilter, limit=offset + cap + _SUMMARY_CANDIDATES,
        )
        ranked_lists.append(_extract_summary_hits(summaries))

        # 4. Fuse via RRF. Dedup by (kind, id) naturally merges chunks that
        #    appear in multiple heads — their scores sum, which is correct.
        fused = _reciprocal_rank_fusion(ranked_lists)

        # 5. Apply offset + cap (+1 to detect truncation).
        page = fused[offset : offset + cap + 1]
        truncated = len(page) > cap
        page = page[:cap]
        if not page:
            return [], None, False

        # 6. Hydrate from Postgres (titles, sensitivity, archived state, etc.)
        items = await self._hydrate(tenant_id, page, filters)

        next_cursor: str | None = None
        if truncated:
            next_cursor = encode_cursor({"offset": offset + cap})
        return items, next_cursor, truncated

    # ─── search helpers ─────────────────────────────────────────────────────

    async def _search_chunks(
        self, *, head: str, qvec: list[float],
        qfilter: qm.Filter, limit: int,
    ) -> list[Any]:
        """Search chunks by a named dense vector head."""
        return cast("list[Any]", await self._qdrant._client.search(
            collection_name="chunks",
            query_vector=qm.NamedVector(name=head, vector=qvec),
            query_filter=qfilter,
            limit=limit,
            with_payload=True,
        ))

    async def _search_chunks_sparse(
        self, *, qsparse: dict[int, float],
        qfilter: qm.Filter, limit: int,
    ) -> list[Any]:
        """Search chunks by sparse (lexical) vector head."""
        indices = list(qsparse.keys())
        values  = [qsparse[k] for k in indices]
        if not indices:
            return []
        return cast("list[Any]", await self._qdrant._client.search(
            collection_name="chunks",
            query_vector=qm.NamedSparseVector(
                name="sparse",
                vector=qm.SparseVector(indices=indices, values=values),
            ),
            query_filter=qfilter,
            limit=limit,
            with_payload=True,
        ))

    async def _search_summaries(
        self, *, qvec: list[float], qfilter: qm.Filter, limit: int,
    ) -> list[Any]:
        """Search summaries by dense head."""
        return cast("list[Any]", await self._qdrant._client.search(
            collection_name="summaries",
            query_vector=("dense", qvec),
            query_filter=qfilter,
            limit=limit,
            with_payload=True,
        ))

    async def _hydrate(
        self, tenant_id: UUID, fused: list[dict[str, Any]],
        filters: Filters,
    ) -> list[ResultItem]:
        """Hydrate matched (doc_id, chunk_id|None) pairs into ResultItem
        with full metadata + provenance. Single SQL query per row class.
        """
        chunk_ids = [m["chunk_id"] for m in fused if m["kind"] == "chunk"]
        summary_doc_ids = [m["doc_id"] for m in fused if m["kind"] == "doc"]

        chunk_rows: dict[str, dict[str, Any]] = {}
        if chunk_ids:
            async with self._pool.tenant_session(tenant_id) as conn:
                rows = await conn.fetch(
                    """
                    SELECT c.chunk_id, c.doc_id, c.text, c.token_count, c.ordinal,
                           d.title, d.filename, d.summary,
                           d.processing_profile, d.sensitivity_class,
                           d.created_at, d.last_accessed_at
                      FROM chunks c
                      JOIN documents d ON d.doc_id = c.doc_id AND d.tenant_id = c.tenant_id
                     WHERE c.tenant_id = $1
                       AND c.chunk_id = ANY ($2::uuid[])
                       AND d.deleted_at IS NULL
                       AND d.archived_at IS NULL
                    """,
                    tenant_id, chunk_ids,
                )
                for r in rows:
                    chunk_rows[str(r["chunk_id"])] = dict(r)

        doc_rows: dict[str, dict[str, Any]] = {}
        if summary_doc_ids:
            async with self._pool.tenant_session(tenant_id) as conn:
                rows = await conn.fetch(
                    """
                    SELECT d.doc_id, d.title, d.filename, d.summary,
                           d.processing_profile, d.sensitivity_class,
                           d.topics_inferred, d.created_at, d.last_accessed_at
                      FROM documents d
                     WHERE d.tenant_id = $1
                       AND d.doc_id = ANY ($2::uuid[])
                       AND d.deleted_at IS NULL
                       AND d.archived_at IS NULL
                    """,
                    tenant_id, summary_doc_ids,
                )
                for r in rows:
                    doc_rows[str(r["doc_id"])] = dict(r)

        # Build ResultItems in fused order; drop sensitivity-filtered out.
        items: list[ResultItem] = []
        for m in fused:
            if m["kind"] == "chunk":
                row = chunk_rows.get(str(m["chunk_id"]))
                if row is None:
                    continue
                if not _passes_sensitivity(row["sensitivity_class"], filters.sensitivity):
                    continue
                items.append(ResultItem(
                    kind="chunk",
                    id=m["chunk_id"],
                    score=m["fused_score"],
                    title=row.get("title"),
                    snippet=(row["text"] or "")[:280],
                    metadata={
                        "doc_id":             str(row["doc_id"]),
                        "ordinal":            row["ordinal"],
                        "filename":           row.get("filename"),
                        "processing_profile": row["processing_profile"],
                        "raw_score":          m.get("raw_score"),
                    },
                    provenance=Provenance(
                        doc_ids=[row["doc_id"]],
                        chunk_ids=[m["chunk_id"]],
                    ),
                    sensitivity_class=row["sensitivity_class"] or "standard",
                    created_at=row.get("created_at"),
                    last_accessed_at=row.get("last_accessed_at"),
                ))
            elif m["kind"] == "doc":
                row = doc_rows.get(str(m["doc_id"]))
                if row is None:
                    continue
                if not _passes_sensitivity(row["sensitivity_class"], filters.sensitivity):
                    continue
                items.append(ResultItem(
                    kind="doc",
                    id=m["doc_id"],
                    score=m["fused_score"],
                    title=row.get("title"),
                    summary=row["summary"],
                    metadata={
                        "filename":           row.get("filename"),
                        "processing_profile": row["processing_profile"],
                        "topics":             list(row["topics_inferred"] or []),
                        "raw_score":          m.get("raw_score"),
                    },
                    provenance=Provenance(doc_ids=[row["doc_id"]]),
                    sensitivity_class=row["sensitivity_class"] or "standard",
                    created_at=row.get("created_at"),
                    last_accessed_at=row.get("last_accessed_at"),
                ))
        return items


# ─── Filter construction ────────────────────────────────────────────────────

def _build_qdrant_filter(tenant_id: UUID, filters: Filters) -> qm.Filter:
    must: list[qm.FieldCondition] = [
        qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
    ]

    if filters.processing_profiles:
        must.append(qm.FieldCondition(
            key="processing_profile",
            match=qm.MatchAny(any=list(filters.processing_profiles)),
        ))

    if filters.scope_ids:
        must.append(qm.FieldCondition(
            key="scope_ids",
            match=qm.MatchAny(any=[str(s) for s in filters.scope_ids]),
        ))

    since, until = resolve_time_filter(filters.time)
    if since is not None:
        must.append(qm.FieldCondition(
            key="valid_time_end",
            range=qm.Range(gte=since.timestamp()),
        ))
    if until is not None:
        must.append(qm.FieldCondition(
            key="valid_time_start",
            range=qm.Range(lte=until.timestamp()),
        ))

    return qm.Filter(must=must)


# ─── Hit extraction ─────────────────────────────────────────────────────────

def _extract_chunk_hits(hits) -> list[dict[str, Any]]:    # type: ignore[no-untyped-def]
    out: list[dict[str, Any]] = []
    for h in hits:
        payload = h.payload or {}
        try:
            chunk_id = UUID(str(payload.get("chunk_id") or h.id))
            doc_id   = UUID(str(payload["doc_id"]))
        except (KeyError, ValueError):
            continue
        out.append({
            "kind":      "chunk",
            "chunk_id":  chunk_id,
            "doc_id":    doc_id,
            "raw_score": float(h.score) if h.score is not None else 0.0,
        })
    return out


def _extract_summary_hits(hits) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    out: list[dict[str, Any]] = []
    for h in hits:
        payload = h.payload or {}
        try:
            doc_id = UUID(str(payload["doc_id"]))
        except (KeyError, ValueError):
            continue
        out.append({
            "kind":      "doc",
            "doc_id":    doc_id,
            "raw_score": float(h.score) if h.score is not None else 0.0,
        })
    return out


# ─── Reciprocal Rank Fusion ────────────────────────────────────────────────

def _reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    *,
    k: int = _RRF_K,
) -> list[dict[str, Any]]:
    """Standard RRF: each item's contribution from a list is 1/(k + rank).
    De-duplicates by (kind, id) — a chunk appearing in multiple heads
    (dense + sparse + colbert) gets its scores summed, which naturally
    boosts items that score well across multiple retrieval strategies.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            key = (item["kind"], str(item.get("chunk_id") or item.get("doc_id")))
            score = 1.0 / (k + rank + 1)
            if key in by_key:
                by_key[key]["fused_score"] += score
                prev = by_key[key].get("raw_score") or 0.0
                if (item.get("raw_score") or 0.0) > prev:
                    by_key[key]["raw_score"] = item["raw_score"]
            else:
                merged = dict(item)
                merged["fused_score"] = score
                by_key[key] = merged

    return sorted(by_key.values(), key=lambda x: x["fused_score"], reverse=True)


# ─── Sensitivity filter (post-search) ───────────────────────────────────────

def _passes_sensitivity(
    sensitivity_class: str | None, filt: SensitivityFilter,
) -> bool:
    sc = sensitivity_class or "standard"
    if filt == SensitivityFilter.ANY:
        return True
    if filt == SensitivityFilter.EXCLUDE_SENSITIVE:
        return sc != "sensitive"
    if filt == SensitivityFilter.ONLY_SENSITIVE:
        return sc == "sensitive"
    return True


# ─── ColBERT query helper ─────────────────────────────────────────────────

def _mean_colbert_query(colbert: list[list[float]]) -> list[float]:
    """Mean-pool the per-token ColBERT query vectors into a single 1024d vector.

    Full multi-vector ColBERT search (MAX_SIM over individual token vectors)
    requires Qdrant 1.14+ on the client side. Until then, we mean-pool the
    query side while the stored chunks retain their per-token multi-vectors.
    When the Qdrant client is upgraded, switch this to native multi-vector
    query — no data migration needed.
    """
    if not colbert:
        return []
    dim = len(colbert[0])
    summed = [0.0] * dim
    for vec in colbert:
        for i, v in enumerate(vec):
            summed[i] += v
    n = len(colbert)
    return [s / n for s in summed]
