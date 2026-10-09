"""Async Qdrant client wrapper.

Thin layer over qdrant-client.AsyncQdrantClient that:
  - Loads settings, prefers gRPC for hot internal hops
  - Exposes upsert helpers for chunks (multi-vector), summaries, scopes
  - Handles tenant_id payload filter consistently
  - Bootstraps collections on demand

Retrieval-side helpers (search, rerank) are out of scope for ingestion.
This module only writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import structlog
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

from woxpas_storage.qdrant.collections import (
    Collection,
    Vec,
)
from woxpas_storage.qdrant.collections import (
    bootstrap as _bootstrap,
)
from woxpas_storage.settings import QdrantSettings

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ChunkPoint:
    """Single chunk's vector payload for chunks_collection upsert."""
    point_id: UUID
    tenant_id: UUID
    doc_id: UUID
    chunk_id: UUID
    scope_ids: list[UUID]
    processing_profile: str
    pipeline_version: str
    valid_time_start: str | None       # ISO 8601
    valid_time_end: str | None
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    colbert: list[list[float]] | None  # list of token vectors; None if not produced


@dataclass(slots=True)
class SummaryPoint:
    point_id: UUID
    tenant_id: UUID
    doc_id: UUID
    scope_ids: list[UUID]
    processing_profile: str
    topics: list[str]
    dense: list[float]


@dataclass(slots=True)
class ScopePoint:
    """Either description or centroid is set (per upsert)."""
    point_id: UUID
    tenant_id: UUID
    scope_id: UUID
    member_count: int
    description_vector: list[float] | None = None
    centroid_vector: list[float] | None = None


class QdrantClient:
    def __init__(self, client: AsyncQdrantClient, settings: QdrantSettings) -> None:
        self._client = client
        self._settings = settings

    @classmethod
    async def from_settings(cls, settings: QdrantSettings) -> QdrantClient:
        client = AsyncQdrantClient(
            host=settings.host,
            port=settings.port,
            grpc_port=settings.grpc_port,
            api_key=settings.api_key.get_secret_value() or None,
            prefer_grpc=settings.prefer_grpc,
            timeout=int(settings.timeout_s),
            https=False,
        )
        log.info("qdrant.client.connected", host=settings.host, grpc=settings.prefer_grpc)
        return cls(client, settings)

    async def close(self) -> None:
        await self._client.close()

    async def bootstrap(self) -> dict[str, str]:
        actions = await _bootstrap(self._client)
        log.info("qdrant.bootstrap", **actions)
        return actions

    async def health(self) -> dict[str, Any]:
        try:
            cols = await self._client.get_collections()
            return {"ok": True, "collections": [c.name for c in cols.collections]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ─── Upsert helpers ───────────────────────────────────────────────────

    async def upsert_chunks(self, points: list[ChunkPoint]) -> None:
        if not points:
            return
        qpoints: list[qm.PointStruct] = []
        for p in points:
            vectors: dict[str, Any] = {
                Vec.DENSE.value:  p.dense,
                Vec.SPARSE.value: qm.SparseVector(indices=p.sparse_indices, values=p.sparse_values),
            }
            if p.colbert is not None:
                vectors[Vec.COLBERT.value] = p.colbert
            qpoints.append(qm.PointStruct(
                id=str(p.point_id),
                vector=vectors,
                payload={
                    "tenant_id":          str(p.tenant_id),
                    "doc_id":             str(p.doc_id),
                    "chunk_id":           str(p.chunk_id),
                    "scope_ids":          [str(s) for s in p.scope_ids],
                    "processing_profile": p.processing_profile,
                    "pipeline_version":   p.pipeline_version,
                    "valid_time_start":   p.valid_time_start,
                    "valid_time_end":     p.valid_time_end,
                },
            ))
        await self._client.upsert(
            collection_name=Collection.CHUNKS.value,
            points=qpoints,
            wait=False,                                   # async commit; durability via Postgres state
        )

    async def upsert_summaries(self, points: list[SummaryPoint]) -> None:
        if not points:
            return
        qpoints = [
            qm.PointStruct(
                id=str(p.point_id),
                vector={Vec.DENSE.value: p.dense},
                payload={
                    "tenant_id":          str(p.tenant_id),
                    "doc_id":             str(p.doc_id),
                    "scope_ids":          [str(s) for s in p.scope_ids],
                    "processing_profile": p.processing_profile,
                    "topics":             p.topics,
                },
            )
            for p in points
        ]
        await self._client.upsert(
            collection_name=Collection.SUMMARIES.value,
            points=qpoints, wait=False,
        )

    async def upsert_scope(self, point: ScopePoint) -> None:
        vectors: dict[str, Any] = {}
        if point.description_vector is not None:
            vectors[Vec.DESCRIPTION.value] = point.description_vector
        if point.centroid_vector is not None:
            vectors[Vec.CENTROID.value] = point.centroid_vector
        if not vectors:
            return
        await self._client.upsert(
            collection_name=Collection.SCOPES.value,
            points=[qm.PointStruct(
                id=str(point.point_id),
                vector=vectors,
                payload={
                    "tenant_id":    str(point.tenant_id),
                    "scope_id":     str(point.scope_id),
                    "member_count": point.member_count,
                },
            )],
            wait=False,
        )

    async def delete_doc_chunks(self, tenant_id: UUID, doc_id: UUID) -> None:
        """Cascade delete when a doc is hard-deleted (soft-delete leaves vectors)."""
        flt = qm.Filter(must=[
            qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
            qm.FieldCondition(key="doc_id",    match=qm.MatchValue(value=str(doc_id))),
        ])
        for col in (Collection.CHUNKS.value, Collection.SUMMARIES.value):
            await self._client.delete(
                collection_name=col,
                points_selector=qm.FilterSelector(filter=flt),
                wait=False,
            )

    # ─── Read paths used by supersession + later retrieval ───────────────

    async def search_scopes_by_vector(
        self,
        tenant_id: UUID,
        query_vector: list[float],
        *,
        limit: int = 5,
        prefer: str = "centroid",        # "centroid" | "description"
    ) -> list[dict[str, Any]]:
        """Find tenant scopes whose centroid (or description) is closest to
        a given vector — used by scope auto-assignment to match a doc summary
        against existing scopes.

        Returns dicts: {point_id, score, scope_id, member_count}.
        Falls back to the alternate vector head if the preferred one is empty
        (e.g., a new scope without member_centroid yet uses description).
        """
        flt = qm.Filter(must=[
            qm.FieldCondition(key="tenant_id",
                              match=qm.MatchValue(value=str(tenant_id))),
        ])
        primary = Vec.CENTROID.value if prefer == "centroid" else Vec.DESCRIPTION.value
        fallback = Vec.DESCRIPTION.value if prefer == "centroid" else Vec.CENTROID.value

        async def _search(head_name: str) -> list[Any]:
            try:
                return await self._client.search(
                    collection_name=Collection.SCOPES.value,
                    query_vector=qm.NamedVector(name=head_name, vector=query_vector),
                    query_filter=flt,
                    limit=limit, with_payload=True,
                )
            except Exception:
                return []

        results = await _search(primary)
        if not results:
            results = await _search(fallback)

        out: list[dict[str, Any]] = []
        for r in results:
            payload = r.payload or {}
            out.append({
                "point_id":     r.id,
                "score":        float(r.score),
                "scope_id":     payload.get("scope_id"),
                "member_count": payload.get("member_count", 0),
            })
        return out

    async def search_similar_summaries(
        self,
        tenant_id: UUID,
        point_id: UUID,
        *,
        limit: int = 5,
        exclude_doc_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Return summaries semantically similar to the given point.

        Looks the point up in summaries_collection, then runs a similarity search
        over the same collection within tenant scope, excluding the source doc.
        Returns the top `limit` non-self matches as dicts:
          {point_id, score, doc_id, scope_ids, processing_profile, topics}
        """
        # 1. Retrieve our vector
        retrieved = await self._client.retrieve(
            collection_name=Collection.SUMMARIES.value,
            ids=[str(point_id)],
            with_vectors=True,
        )
        if not retrieved:
            return []
        vec_field = retrieved[0].vector
        if isinstance(vec_field, dict):
            our_vec = vec_field.get(Vec.DENSE.value)
        else:
            our_vec = vec_field
        if our_vec is None:
            return []

        must_not: list[qm.Condition] = []
        if exclude_doc_id is not None:
            must_not.append(qm.FieldCondition(
                key="doc_id", match=qm.MatchValue(value=str(exclude_doc_id))))

        flt = qm.Filter(
            must=[qm.FieldCondition(key="tenant_id",
                                    match=qm.MatchValue(value=str(tenant_id)))],
            must_not=must_not or None,
        )

        results = await self._client.search(
            collection_name=Collection.SUMMARIES.value,
            query_vector=qm.NamedVector(
                name=Vec.DENSE.value, vector=cast("list[float]", our_vec),
            ),
            query_filter=flt,
            limit=limit + 1,                     # +1 in case self comes back
            with_payload=True,
        )
        out: list[dict[str, Any]] = []
        for r in results:
            payload = r.payload or {}
            if exclude_doc_id and payload.get("doc_id") == str(exclude_doc_id):
                continue
            out.append({
                "point_id":           r.id,
                "score":              float(r.score),
                "doc_id":             payload.get("doc_id"),
                "scope_ids":          payload.get("scope_ids", []),
                "processing_profile": payload.get("processing_profile"),
                "topics":             payload.get("topics", []),
            })
            if len(out) >= limit:
                break
        return out
