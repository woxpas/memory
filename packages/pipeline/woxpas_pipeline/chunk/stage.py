"""Chunk stage — block aggregation, span generation.

Subscribes to doc.normalized (parallel with summarize). Reads canonical_blocks,
aggregates into chunks (no overlap, target 768 / hard max 1024 tokens), generates
sentence-level spans. Bulk-inserts both. Emits doc.chunked so embed+index can run.

Embedding is a separate stage; chunks land here with their qdrant_point_id set
but no vector yet. The embed/index stage fills the vectors and flips the doc
to status='indexed'.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import (
    ChunkRepo,
    DocScopeRepo,
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
    SpanRepo,
    TimelineRepo,
)

from woxpas_pipeline.chunk.splitter import build_chunks, to_core_chunks_and_spans
from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed

log = structlog.get_logger(__name__)


async def _publish_doc_chunked(  # local helper since it's the only consumer for now
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    chunk_count: int, span_count: int,
    correlation_id: UUID | None = None, causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_CHUNKED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={"chunk_count": chunk_count, "span_count": span_count},
    ))


class ChunkStage:
    name = "chunk"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("chunk"))

    def event_type(self) -> EventType:
        return EventType.DOC_NORMALIZED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        doc_id    = event.aggregate_id
        pv        = self._ctx.pipeline.pipeline_version

        # Load doc + blocks + scopes under tenant RLS, open a run.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo  = DocumentRepo(conn)
            run_repo  = PipelineRunRepo(conn)
            scope_repo = DocScopeRepo(conn)

            doc = await doc_repo.get(tenant_id, doc_id)
            if doc is None:
                log.warning("chunk.doc_missing", doc_id=str(doc_id))
                return
            run_id = await run_repo.start(tenant_id, doc_id, self.name, pv)
            scope_ids = await scope_repo.scopes_for_doc(tenant_id, doc_id)

            blocks = await conn.fetch(
                """
                SELECT block_id, doc_id, tenant_id, ordinal, type, level,
                       text, source_locator
                  FROM canonical_blocks
                 WHERE tenant_id = $1 AND doc_id = $2
                 ORDER BY ordinal
                """,
                tenant_id, doc_id,
            )

        if not blocks:
            log.warning("chunk.no_blocks", doc_id=str(doc_id))
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                run_repo = PipelineRunRepo(conn)
                await run_repo.skip(run_id, "no_blocks")
            return

        # Build chunks + spans (CPU work — wrap in to_thread so the event loop stays free).
        try:
            built = await asyncio.to_thread(
                build_chunks,
                [dict(b) for b in blocks],
                target_tokens=self._ctx.pipeline.chunk_target_tokens,
                hard_max_tokens=self._ctx.pipeline.chunk_hard_max_tokens,
                min_tokens=self._ctx.pipeline.chunk_min_tokens,
                language=doc.get("language") or doc.get("language_detected"),
            )
        except Exception as e:
            log.error("chunk.build_failed", doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "chunk_build_error", str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        if not built:
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                run_repo = PipelineRunRepo(conn)
                await run_repo.skip(run_id, "empty_after_build")
            return

        # Embedder model identity (stamped on chunks for selective re-embed).
        try:
            embedder = await self._ctx.embedder()
            embed_model = embedder.model_id
            embed_model_version = embedder.model_version
        except Exception:
            # No embedder available — still persist chunks (embed stage will fill later).
            embed_model = "deferred"
            embed_model_version = "deferred@0"

        chunks_core, spans_core = to_core_chunks_and_spans(
            built,
            doc_id=doc_id,
            tenant_id=tenant_id,
            embed_model=embed_model,
            embed_model_version=embed_model_version,
            processing_profile=doc["processing_profile"],
            pipeline_version=pv,
            valid_time_start=None,            # bitemporal hint set by extraction later
            valid_time_end=None,
            scope_ids=scope_ids,
        )

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            chunk_repo = ChunkRepo(conn)
            span_repo  = SpanRepo(conn)
            outbox     = OutboxRepo(conn)
            timeline   = TimelineRepo(conn)
            run_repo   = PipelineRunRepo(conn)
            doc_repo   = DocumentRepo(conn)

            n_chunks = await chunk_repo.bulk_insert(chunks_core)
            n_spans  = await span_repo.bulk_insert(tenant_id, spans_core)

            # documents.status is forward-only and reflects the highest milestone.
            # 'normalized' → 'indexed' is the only post-CDM transition. Chunk and
            # summarize branches don't change status; pipeline_runs has per-stage truth
            # so a scheduler can query for stuck docs by stage state.

            await run_repo.succeed(run_id, metrics={
                "chunks": n_chunks, "spans": n_spans,
                "blocks": len(blocks),
                "target_tokens": self._ctx.pipeline.chunk_target_tokens,
                "hard_max_tokens": self._ctx.pipeline.chunk_hard_max_tokens,
            })
            await timeline.record(
                tenant_id, "doc.indexed",          # closest schema event_type for "doc state changed"
                doc_id=doc_id, actor="system",
                payload={"stage": "chunked", "chunks": n_chunks, "spans": n_spans},
            )
            await _publish_doc_chunked(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id, pipeline_version=pv,
                chunk_count=n_chunks, span_count=n_spans,
                causation_id=event.correlation_id,
            )

        log.info("chunk.ok",
                 doc_id=str(doc_id),
                 chunks=n_chunks, spans=n_spans,
                 blocks=len(blocks))

    async def _record_failure(
        self, tenant_id: UUID, doc_id: UUID, run_id: UUID, error_class: str,
        message: str, retriable: bool, causation_id: UUID | None,
    ) -> None:
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
