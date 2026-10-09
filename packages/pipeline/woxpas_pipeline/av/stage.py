"""AV stage — scans a doc's raw bytes via ClamAV; updates av_result;
emits doc.scanned (or doc.failed if infected/error)."""

from __future__ import annotations

import asyncio

import structlog
from woxpas_core import AVResult, DocStatus, Event, EventType
from woxpas_storage import (
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
    TimelineRepo,
)

from woxpas_pipeline.av.clamav import ClamAVClient, ScanResult
from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed, publish_doc_scanned

log = structlog.get_logger(__name__)


class AvStage:
    """Subscribes to ingest.received."""

    name = "av_scan"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._clamav = ClamAVClient(ctx.clamav)
        self._sem = asyncio.Semaphore(ctx.concurrency("av"))

    def event_type(self) -> EventType:
        return EventType.INGEST_RECEIVED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        doc_id    = event.aggregate_id

        # Read raw bytes from object store (decrypt) — re-fetch by sha256 from doc row.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            run_repo = PipelineRunRepo(conn)
            doc = await doc_repo.get(tenant_id, doc_id)
            if doc is None:
                log.warning("av.doc_missing", doc_id=str(doc_id))
                return
            run_id = await run_repo.start(
                tenant_id, doc_id, self.name, self._ctx.pipeline.pipeline_version,
            )

        sha256_hex = doc["sha256"].hex() if isinstance(doc["sha256"], (bytes, bytearray)) \
                     else str(doc["sha256"])
        try:
            dek = await self._ctx.session_dek(tenant_id)
            content = await self._ctx.objstore.get_raw_bytes(sha256_hex, dek)
        except Exception as e:
            log.error("av.fetch_failed", doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "fetch_error", str(e), retriable=True,
                causation_id=event.correlation_id,
            )
            return

        try:
            report = await self._clamav.scan_bytes(content)
        except Exception as e:
            log.error("av.scan_exception", doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "scan_exception", str(e), retriable=True,
                causation_id=event.correlation_id,
            )
            return

        # Map ClamAV result → schema enum
        if report.result is ScanResult.CLEAN:
            av_value = AVResult.CLEAN.value
        elif report.result is ScanResult.INFECTED:
            av_value = AVResult.INFECTED.value
        else:
            av_value = AVResult.ERROR.value

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            outbox   = OutboxRepo(conn)
            timeline = TimelineRepo(conn)
            run_repo = PipelineRunRepo(conn)

            await doc_repo.update_av_result(
                tenant_id, doc_id, av_value, report.signature,
            )

            if report.result is ScanResult.INFECTED:
                # Quarantined: stop the pipeline here. Surfacing/UI can show this.
                await run_repo.fail(
                    run_id, "infected", report.signature or "unknown",
                    retriable=False,
                    metrics={"signature": report.signature},
                )
                await timeline.record(
                    tenant_id, "doc.ingested",   # surface as a state-change event
                    doc_id=doc_id, actor="system",
                    payload={"av_result": av_value, "signature": report.signature},
                )
                await publish_doc_failed(
                    outbox,
                    tenant_id=tenant_id, doc_id=doc_id,
                    pipeline_version=self._ctx.pipeline.pipeline_version,
                    stage=self.name, error_class="infected",
                    error_message=report.signature or "av infected",
                    retriable=False, causation_id=event.correlation_id,
                    extra={"av_signature": report.signature},
                )
                log.warning("av.infected",
                            doc_id=str(doc_id), signature=report.signature)
                return

            if report.result is ScanResult.ERROR:
                await run_repo.fail(
                    run_id, "av_error", report.raw_response or "av error",
                    retriable=True,
                )
                await publish_doc_failed(
                    outbox,
                    tenant_id=tenant_id, doc_id=doc_id,
                    pipeline_version=self._ctx.pipeline.pipeline_version,
                    stage=self.name, error_class="av_error",
                    error_message=report.raw_response or "av error",
                    retriable=True, causation_id=event.correlation_id,
                )
                return

            # CLEAN: advance.
            await doc_repo.update_status(tenant_id, doc_id, DocStatus.EXTRACTING)
            await run_repo.succeed(run_id, metrics={"av_result": "clean"})
            await publish_doc_scanned(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                av_result=av_value,
                causation_id=event.correlation_id,
            )

        log.info("av.clean", doc_id=str(doc_id))

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
