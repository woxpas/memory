"""Supersession candidate stage (Type-1 conflict pre-pass).

Subscribes to doc.summarized. When a new doc lands and we have its summary
embedding, we compare against existing summaries in the same tenant. If the
top match is similar enough (cosine > threshold) AND was created within a
recent window, we flag it as a supersession candidate.

This is the cheap, fast pre-pass. The extraction pipeline (built later) will
confirm or reject these candidates with deeper analysis (entity overlap,
content similarity at chunk level, explicit revision markers, etc.) and
finalize `documents.supersedes_doc_id` from `supersedes_candidate_doc_id`.

For v0.1, false positives are acceptable here — extraction can dismiss them.
False negatives are also acceptable — extraction may detect supersession
through other signals.

Threshold and window are configurable via PipelineSettings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import DocumentRepo, OutboxRepo, PipelineRunRepo, TimelineRepo

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed

log = structlog.get_logger(__name__)


class SupersessionStage:
    name = "supersession_check"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(2)         # not heavy; small fixed bound

    def event_type(self) -> EventType:
        return EventType.DOC_SUMMARIZED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        doc_id    = event.aggregate_id
        pv        = self._ctx.pipeline.pipeline_version
        threshold = self._ctx.pipeline.supersession_similarity_threshold
        window_s  = self._ctx.pipeline.supersession_window_seconds

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            run_repo = PipelineRunRepo(conn)
            doc = await doc_repo.get(tenant_id, doc_id)
            if doc is None:
                log.warning("supersession.doc_missing", doc_id=str(doc_id))
                return
            run_id = await run_repo.start(tenant_id, doc_id, self.name, pv)

        embedding_ref: UUID | None = doc.get("summary_embedding_ref")
        if embedding_ref is None:
            # Tiny-doc shortcut path produced a summary without embedding —
            # nothing to compare against. Skip rather than error.
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                run_repo = PipelineRunRepo(conn)
                await run_repo.skip(run_id, "no_summary_embedding")
            log.info("supersession.skipped_no_embedding", doc_id=str(doc_id))
            return

        try:
            candidates = await self._ctx.qdrant.search_similar_summaries(
                tenant_id, embedding_ref,
                limit=5, exclude_doc_id=doc_id,
            )
        except Exception as e:
            log.error("supersession.qdrant_search_failed",
                      doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "qdrant_search_failed", str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # Filter by similarity threshold + age. Only consider matches that are
        # recent (could plausibly be a revision) and similar.
        cutoff = datetime.now(UTC) - timedelta(seconds=window_s)
        chosen: dict | None = None
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            for cand in candidates:
                if cand["score"] < threshold:
                    continue
                cand_doc_id = cand.get("doc_id")
                if not cand_doc_id:
                    continue
                cand_doc = await conn.fetchrow(
                    "SELECT doc_id, created_at, source_channel, captured_by, thread_id "
                    "  FROM documents WHERE tenant_id=$1 AND doc_id=$2 AND deleted_at IS NULL",
                    tenant_id, UUID(cand_doc_id),
                )
                if cand_doc is None:
                    continue
                if cand_doc["created_at"] < cutoff:
                    continue
                # Stronger signal: same source_channel + same captured_by.
                same_origin = (
                    cand_doc["source_channel"] == doc["source_channel"] and
                    cand_doc["captured_by"]    == doc["captured_by"]
                )
                # Even stronger: same thread.
                same_thread = (
                    doc.get("thread_id") is not None and
                    cand_doc["thread_id"] == doc["thread_id"]
                )
                # For v0.1, accept any same-origin candidate within window above threshold;
                # log the strength signals for later tuning.
                if same_origin or same_thread:
                    chosen = {
                        "doc_id": cand_doc["doc_id"],
                        "score": cand["score"],
                        "same_origin": same_origin,
                        "same_thread": same_thread,
                    }
                    break

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            run_repo = PipelineRunRepo(conn)
            timeline = TimelineRepo(conn)
            if chosen is not None:
                await doc_repo.set_supersession_candidate(
                    tenant_id, doc_id, chosen["doc_id"],
                )
                await timeline.record(
                    tenant_id, "doc.summarized",
                    doc_id=doc_id, actor="system",
                    payload={
                        "stage": "supersession_check",
                        "candidate_doc_id": str(chosen["doc_id"]),
                        "score": chosen["score"],
                        "same_origin": chosen["same_origin"],
                        "same_thread": chosen["same_thread"],
                    },
                )
                log.info("supersession.candidate_set",
                         doc_id=str(doc_id),
                         candidate_doc_id=str(chosen["doc_id"]),
                         score=round(chosen["score"], 4),
                         same_thread=chosen["same_thread"])
                await run_repo.succeed(run_id, metrics={
                    "candidates_evaluated": len(candidates),
                    "candidate_score":      chosen["score"],
                    "threshold":            threshold,
                    "window_seconds":       window_s,
                })
            else:
                await run_repo.succeed(run_id, metrics={
                    "candidates_evaluated": len(candidates),
                    "best_score": max((c["score"] for c in candidates), default=0.0),
                    "threshold":  threshold,
                })
                log.info("supersession.no_candidate",
                         doc_id=str(doc_id),
                         evaluated=len(candidates))

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
