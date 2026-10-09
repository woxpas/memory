"""Detect stage — sniffs MIME from raw bytes; updates documents.mime if changed;
emits doc.detected with the suggested processing profile.

Subscribes to doc.scanned. Skipped on infected/error scans (those route to doc.failed).
"""

from __future__ import annotations

import asyncio

import structlog
from woxpas_core import Event, EventType, ProcessingProfile
from woxpas_storage import (
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
)

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.detect.magic import detect_bytes
from woxpas_pipeline.events import publish_doc_detected, publish_doc_failed

log = structlog.get_logger(__name__)


class DetectStage:
    name = "type_detect"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("detect"))

    def event_type(self) -> EventType:
        return EventType.DOC_SCANNED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        doc_id    = event.aggregate_id

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            run_repo = PipelineRunRepo(conn)
            doc = await doc_repo.get(tenant_id, doc_id)
            if doc is None:
                log.warning("detect.doc_missing", doc_id=str(doc_id))
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
            log.error("detect.fetch_failed", doc_id=str(doc_id), error=str(e))
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                run_repo = PipelineRunRepo(conn)
                outbox   = OutboxRepo(conn)
                await run_repo.fail(run_id, "fetch_error", str(e), retriable=True)
                await publish_doc_failed(
                    outbox,
                    tenant_id=tenant_id, doc_id=doc_id,
                    pipeline_version=self._ctx.pipeline.pipeline_version,
                    stage=self.name, error_class="fetch_error",
                    error_message=str(e), retriable=True,
                    causation_id=event.correlation_id,
                )
            return

        result = detect_bytes(content, filename=doc.get("filename"))

        # Reconcile profile suggestion. If the doc already has a non-default profile
        # (set explicitly by the user/agent), don't override; classifier may refine later.
        current_profile: str = doc["processing_profile"]
        new_profile_value = current_profile
        if current_profile == ProcessingProfile.PERSONAL.value and result.profile_suggested:
            new_profile_value = result.profile_suggested

        # Persist any updates inside one tx with the outbox emit.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            outbox   = OutboxRepo(conn)
            run_repo = PipelineRunRepo(conn)

            if doc.get("mime") != result.mime or new_profile_value != current_profile:
                await conn.execute(
                    """
                    UPDATE documents
                       SET mime = $3,
                           processing_profile = $4,
                           updated_at = now()
                     WHERE tenant_id = $1 AND doc_id = $2
                    """,
                    tenant_id, doc_id, result.mime, new_profile_value,
                )

            await run_repo.succeed(run_id, metrics={
                "mime_detected": result.mime,
                "profile_suggested": result.profile_suggested,
            })

            await publish_doc_detected(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                mime=result.mime,
                profile_suggested=new_profile_value,
                causation_id=event.correlation_id,
            )

        log.info("detect.ok",
                 doc_id=str(doc_id),
                 mime=result.mime, profile=new_profile_value)
