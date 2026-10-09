"""Embed + index stage — final ingestion step.

Subscribes to doc.chunked. Reads the chunks for the doc, batches them through
the embedder, upserts vectors into Qdrant chunks_collection. On full success,
flips documents.status to 'indexed' and emits doc.indexed.

Failure semantics (per the user-confirmed contract):
  - Status is forward-only and only advances to 'indexed' on full success.
  - Qdrant upsert failures leave status at 'normalized' so a scheduler can
    find stuck docs (status='normalized' AND age > N min) and re-trigger.
  - The pipeline_runs row records the per-stage failure with retriable info.
  - Outbox auto-retry (visibility timeout) covers transient failures.
"""

from __future__ import annotations

import asyncio

import structlog
from woxpas_core import DocStatus, Event, EventType
from woxpas_storage import (
    ChunkPoint,
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
    TimelineRepo,
)

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed, publish_doc_indexed

log = structlog.get_logger(__name__)


# Embedder batch size. ONNX BGE-M3 dense on CPU handles ~8 inputs/batch
# comfortably without memory pressure. Configurable via env if needed.
DEFAULT_EMBED_BATCH_SIZE = 8


class EmbedIndexStage:
    name = "embed"   # matches pipeline_runs CHECK constraint

    def __init__(self, ctx: PipelineContext, *, batch_size: int = DEFAULT_EMBED_BATCH_SIZE) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("embed"))
        self._batch_size = batch_size

    def event_type(self) -> EventType:
        return EventType.DOC_CHUNKED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        doc_id    = event.aggregate_id
        pv        = self._ctx.pipeline.pipeline_version

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            run_repo = PipelineRunRepo(conn)
            doc = await doc_repo.get(tenant_id, doc_id)
            if doc is None:
                log.warning("embed.doc_missing", doc_id=str(doc_id))
                return
            run_id = await run_repo.start(tenant_id, doc_id, self.name, pv)
            chunk_rows = await conn.fetch(
                """
                SELECT chunk_id, ordinal, text, qdrant_point_id,
                       embed_model, embed_model_version,
                       scope_ids, processing_profile,
                       valid_time_start, valid_time_end
                  FROM chunks
                 WHERE tenant_id = $1 AND doc_id = $2
                 ORDER BY ordinal
                """,
                tenant_id, doc_id,
            )

        if not chunk_rows:
            log.warning("embed.no_chunks", doc_id=str(doc_id))
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                run_repo = PipelineRunRepo(conn)
                await run_repo.skip(run_id, "no_chunks")
            return

        # Embed all chunks. Embedder runs in worker thread; awaits don't block loop.
        try:
            embedder = await self._ctx.embedder()
            texts = [r["text"] for r in chunk_rows]
            batch = await embedder.embed(texts, batch_size=self._batch_size)
        except Exception as e:
            log.error("embed.embed_failed", doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "embed_failed", str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        if len(batch.dense) != len(chunk_rows):
            err = f"embedder returned {len(batch.dense)} vectors for {len(chunk_rows)} chunks"
            log.error("embed.shape_mismatch", doc_id=str(doc_id), error=err)
            await self._record_failure(
                tenant_id, doc_id, run_id, "shape_mismatch", err,
                retriable=False, causation_id=event.correlation_id,
            )
            return

        # Build ChunkPoints — one per chunk row, vector + payload.
        points: list[ChunkPoint] = []
        for idx, (row, dense) in enumerate(zip(chunk_rows, batch.dense, strict=True)):
            # Sparse: flatten {token_id → weight} dict into parallel arrays.
            sp = batch.sparse[idx] if idx < len(batch.sparse) else {}
            if sp:
                sparse_indices = list(sp.keys())
                sparse_values  = [sp[k] for k in sparse_indices]
            else:
                sparse_indices = []
                sparse_values  = []

            # ColBERT: per-token vectors (CLS already excluded by embedder).
            cb = batch.colbert[idx] if idx < len(batch.colbert) else []
            colbert = cb if cb else None

            points.append(ChunkPoint(
                point_id=row["qdrant_point_id"],
                tenant_id=tenant_id,
                doc_id=doc_id,
                chunk_id=row["chunk_id"],
                scope_ids=list(row["scope_ids"] or []),
                processing_profile=row["processing_profile"],
                pipeline_version=pv,
                valid_time_start=_iso_or_none(row["valid_time_start"]),
                valid_time_end=_iso_or_none(row["valid_time_end"]),
                dense=dense,
                sparse_indices=sparse_indices,
                sparse_values=sparse_values,
                colbert=colbert,
            ))

        # Upsert to Qdrant. If this fails, status stays at 'normalized' and the
        # outbox will retry the doc.chunked event after the visibility timeout.
        try:
            await self._ctx.qdrant.upsert_chunks(points)
        except Exception as e:
            log.error("embed.qdrant_upsert_failed", doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "qdrant_upsert_failed", str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # All vectors upserted. Flip status to 'indexed' and emit terminal event.
        embed_model         = chunk_rows[0]["embed_model"]
        embed_model_version = chunk_rows[0]["embed_model_version"]

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            outbox   = OutboxRepo(conn)
            timeline = TimelineRepo(conn)
            run_repo = PipelineRunRepo(conn)

            await doc_repo.update_status(tenant_id, doc_id, DocStatus.INDEXED)
            await run_repo.succeed(run_id, metrics={
                "chunks": len(points),
                "embed_model": embed_model,
                "embed_model_version": embed_model_version,
                "batch_size": self._batch_size,
            })
            await timeline.record(
                tenant_id, "doc.indexed",
                doc_id=doc_id, actor="system",
                payload={
                    "chunk_count": len(points),
                    "embed_model_version": embed_model_version,
                },
            )
            await publish_doc_indexed(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id, pipeline_version=pv,
                chunk_count=len(points),
                embed_model=embed_model, embed_model_version=embed_model_version,
                causation_id=event.correlation_id,
            )

        log.info("embed.ok",
                 doc_id=str(doc_id),
                 chunks=len(points),
                 embed_model=embed_model_version)

    async def _record_failure(
        self, tenant_id, doc_id, run_id, error_class, message, retriable, causation_id,
    ):
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_repo = PipelineRunRepo(conn)
            outbox   = OutboxRepo(conn)
            await run_repo.fail(run_id, error_class, message, retriable=retriable)
            await publish_doc_failed(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                stage=self.name, error_class=error_class, error_message=message,
                retriable=retriable, causation_id=causation_id,
            )


def _iso_or_none(v) -> str | None:  # type: ignore[no-untyped-def]
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
