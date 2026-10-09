"""Qdrant collection schemas + bootstrap.

Four collections (only the first three are populated by ingestion; entities is a
slot for the extraction pipeline):

  chunks_collection      — BGE-M3 multi-vector (dense + sparse + ColBERT)
  summaries_collection   — dense only (tier-zero index)
  scopes_collection      — description vector + member centroid (semantic dedup)
  entities_collection    — entity name vectors (extraction populates)

Multi-tenancy is enforced via payload filter on tenant_id (mirrored alongside
Postgres RLS). Quantization is scalar int8 by default — ~3-4× speedup on cosine
search at ~1pp recall cost.

Bootstrap is idempotent: re-running creates only what's missing.
"""

from __future__ import annotations

from enum import StrEnum

import structlog
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

log = structlog.get_logger(__name__)

# Embedding dimensionality of BGE-M3 (dense head). Update if model changes.
DENSE_DIM = 1024


class Collection(StrEnum):
    CHUNKS    = "chunks"
    SUMMARIES = "summaries"
    SCOPES    = "scopes"
    ENTITIES  = "entities"


# Vector head names (multi-vector mode in Qdrant).
class Vec(StrEnum):
    DENSE       = "dense"
    SPARSE      = "sparse"
    COLBERT     = "colbert"
    DESCRIPTION = "description"
    CENTROID    = "centroid"
    NAME        = "name"


def _scalar_quant() -> qm.ScalarQuantization:
    return qm.ScalarQuantization(
        scalar=qm.ScalarQuantizationConfig(
            type=qm.ScalarType.INT8, always_ram=True, quantile=0.99,
        )
    )


def _hnsw() -> qm.HnswConfigDiff:
    return qm.HnswConfigDiff(m=32, ef_construct=256, full_scan_threshold=10_000)


def _common_optimizers() -> qm.OptimizersConfigDiff:
    return qm.OptimizersConfigDiff(
        default_segment_number=2,
        indexing_threshold=20_000,
        flush_interval_sec=5,
    )


# ─── Per-collection schemas ─────────────────────────────────────────────────

CHUNKS_VECTORS = {
    Vec.DENSE.value:   qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE,
                                        on_disk=True, hnsw_config=_hnsw()),
    Vec.COLBERT.value: qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE,
                                        multivector_config=qm.MultiVectorConfig(
                                            comparator=qm.MultiVectorComparator.MAX_SIM),
                                        on_disk=True),
}
CHUNKS_SPARSE = {
    Vec.SPARSE.value: qm.SparseVectorParams(
        index=qm.SparseIndexParams(on_disk=True),
        modifier=qm.Modifier.IDF,
    ),
}

SUMMARIES_VECTORS = {
    Vec.DENSE.value: qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE,
                                      hnsw_config=_hnsw()),
}

SCOPES_VECTORS = {
    Vec.DESCRIPTION.value: qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE),
    Vec.CENTROID.value:    qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE),
}

ENTITIES_VECTORS = {
    Vec.NAME.value: qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE),
}


# ─── Payload schema (drives indexed payload fields for filter pushdown) ────

# Each entry: (field_name, field_schema)
PAYLOAD_INDEXES: dict[str, list[tuple[str, qm.PayloadSchemaType]]] = {
    Collection.CHUNKS: [
        ("tenant_id",          qm.PayloadSchemaType.KEYWORD),
        ("doc_id",             qm.PayloadSchemaType.KEYWORD),
        ("scope_ids",          qm.PayloadSchemaType.KEYWORD),
        ("processing_profile", qm.PayloadSchemaType.KEYWORD),
        ("pipeline_version",   qm.PayloadSchemaType.KEYWORD),
        ("valid_time_start",   qm.PayloadSchemaType.DATETIME),
        ("valid_time_end",     qm.PayloadSchemaType.DATETIME),
    ],
    Collection.SUMMARIES: [
        ("tenant_id",          qm.PayloadSchemaType.KEYWORD),
        ("doc_id",             qm.PayloadSchemaType.KEYWORD),
        ("scope_ids",          qm.PayloadSchemaType.KEYWORD),
        ("processing_profile", qm.PayloadSchemaType.KEYWORD),
        ("topics",             qm.PayloadSchemaType.KEYWORD),
    ],
    Collection.SCOPES: [
        ("tenant_id",    qm.PayloadSchemaType.KEYWORD),
        ("scope_id",     qm.PayloadSchemaType.KEYWORD),
        ("member_count", qm.PayloadSchemaType.INTEGER),
    ],
    Collection.ENTITIES: [
        ("tenant_id", qm.PayloadSchemaType.KEYWORD),
        ("entity_id", qm.PayloadSchemaType.KEYWORD),
        ("type",      qm.PayloadSchemaType.KEYWORD),
    ],
}


async def bootstrap(client: AsyncQdrantClient) -> dict[str, str]:
    """Idempotent: create collections + payload indexes if missing.

    Returns {collection: action} where action is 'created' or 'exists'.
    """
    actions: dict[str, str] = {}
    existing = {c.name for c in (await client.get_collections()).collections}

    async def _create(name: str, vectors: dict, sparse: dict | None = None) -> None:
        if name in existing:
            actions[name] = "exists"
            return
        kwargs: dict = dict(
            collection_name=name,
            vectors_config=vectors,
            optimizers_config=_common_optimizers(),
            quantization_config=_scalar_quant(),
            on_disk_payload=True,
        )
        if sparse:
            kwargs["sparse_vectors_config"] = sparse
        await client.create_collection(**kwargs)
        actions[name] = "created"

    await _create(Collection.CHUNKS,    CHUNKS_VECTORS, CHUNKS_SPARSE)
    await _create(Collection.SUMMARIES, SUMMARIES_VECTORS)
    await _create(Collection.SCOPES,    SCOPES_VECTORS)
    await _create(Collection.ENTITIES,  ENTITIES_VECTORS)

    # Ensure payload indexes (idempotent at the API level).
    for collection, fields in PAYLOAD_INDEXES.items():
        for field_name, schema in fields:
            try:
                await client.create_payload_index(
                    collection_name=collection.value,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception:
                # Already exists or non-fatal; bootstrap should be idempotent.
                # Logged at debug so a real failure (e.g. connection error) is
                # still traceable without noise on the normal "already exists" path.
                log.debug(
                    "qdrant.payload_index_skipped",
                    collection=collection.value, field=field_name, exc_info=True,
                )

    return actions
