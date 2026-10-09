"""Extract stage — runs the right extractor for a doc.detected event,
persists the CanonicalDoc (encrypted JSON to objstore + canonical_blocks rows),
and emits doc.extracted + doc.normalized.

Subscribes to doc.detected. Routes by MIME via the extractor registry. Unknown
MIME → doc.failed (retriable=False; needs an extractor or a profile change).
"""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import structlog
from woxpas_core import DocStatus, Event, EventType
from woxpas_storage import (
    CanonicalBlockRepo,
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
    TimelineRepo,
)

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import (
    publish_doc_extracted,
    publish_doc_failed,
    publish_doc_normalized,
)
from woxpas_pipeline.extract.base import ExtractInput, ExtractResult
from woxpas_pipeline.extract.registry import ExtractorRegistry, build_default_registry
from woxpas_pipeline.sensitivity import HardHit, hard_tier_scan

log = structlog.get_logger(__name__)

# CDM is versioned by an integer alongside the JSON object key. v1 = first persist.
_INITIAL_CDM_VERSION = 1

# Cap the heuristic scan input to avoid pathological cost on huge docs.
# 64K chars covers all-but-the-largest extractions; rare overflow is fine
# since extraction-stage scan is one of two heuristic passes (post-summarize
# refine runs Tier 3 again with a different scope).
_HEURISTIC_SCAN_CAP_CHARS = 65_536


def _scan_canonical_blocks(blocks) -> list[HardHit]:  # type: ignore[no-untyped-def]
    """Concat block text (capped) and run hard-tier scan. Pure function."""
    parts: list[str] = []
    total = 0
    for b in blocks:
        text = (getattr(b, "text", None) or "").strip()
        if not text:
            continue
        if total + len(text) > _HEURISTIC_SCAN_CAP_CHARS:
            parts.append(text[:_HEURISTIC_SCAN_CAP_CHARS - total])
            break
        parts.append(text)
        total += len(text)
    if not parts:
        return []
    return hard_tier_scan("\n".join(parts))


class ExtractStage:
    name = "extract"

    def __init__(
        self,
        ctx: PipelineContext,
        registry: ExtractorRegistry | None = None,
        concurrency: int = 4,
    ) -> None:
        self._ctx = ctx
        self._registry = registry or build_default_registry()
        self._sem = asyncio.Semaphore(concurrency)

    def event_type(self) -> EventType:
        return EventType.DOC_DETECTED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id: UUID = event.tenant_id
        doc_id:    UUID = event.aggregate_id

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc = await DocumentRepo(conn).get(tenant_id, doc_id)
            if doc is None:
                log.warning("extract.doc_missing", doc_id=str(doc_id))
                return
            run_id = await PipelineRunRepo(conn).start(
                tenant_id, doc_id, self.name, self._ctx.pipeline.pipeline_version,
            )

        mime = doc.get("mime") or "application/octet-stream"
        extractor = self._registry.for_mime(mime)
        if extractor is None:
            await self._fail(
                tenant_id, doc_id, run_id,
                error_class="no_extractor", error_message=f"no extractor for mime: {mime}",
                retriable=False, causation_id=event.correlation_id,
            )
            return

        sha256_hex = doc["sha256"].hex() if isinstance(doc["sha256"], (bytes, bytearray)) \
                     else str(doc["sha256"])
        try:
            dek = await self._ctx.session_dek(tenant_id)
            content = await self._ctx.objstore.get_raw_bytes(sha256_hex, dek)
        except Exception as e:
            await self._fail(
                tenant_id, doc_id, run_id,
                error_class="fetch_error", error_message=str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # Run the extractor in a worker thread — many extractors are CPU-bound /
        # call out to C and shouldn't block the event loop.
        started = time.monotonic()
        try:
            inp = ExtractInput(
                doc_id=doc_id,
                tenant_id=tenant_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                content=content,
                mime=mime,
                filename=doc.get("filename"),
                declared_title=doc.get("title"),
                declared_language=doc.get("language"),
            )
            # Audio extractor needs runtime model access → uses async path that takes ctx.
            # Other extractors are pure functions, run them in a worker thread.
            if hasattr(extractor, "extract_async"):
                result = await extractor.extract_async(inp, self._ctx)
            else:
                result = await asyncio.to_thread(extractor.extract, inp)
            result: ExtractResult                                        # type: ignore[no-redef]
        except Exception as e:
            log.error("extract.extractor_failed",
                      doc_id=str(doc_id), extractor=extractor.name, error=str(e))
            await self._fail(
                tenant_id, doc_id, run_id,
                error_class="extractor_error", error_message=str(e),
                retriable=False, causation_id=event.correlation_id,
            )
            return

        duration_ms = int((time.monotonic() - started) * 1000)

        # Tier 3a — fast hard-heuristic sensitivity scan over extracted text.
        # Runs BEFORE doc.normalized fires so the summarize-stage cloud gate
        # can act on the upgraded flag. Hard tier only (precise patterns:
        # IBAN/BIC/CC-Luhn/SSN/ICD-10/medication-names) — soft tier + summary
        # topic semantics live in the post-summarize sensitivity_refine stage.
        hard_hits: list[HardHit] = _scan_canonical_blocks(result.canonical_doc.blocks)

        # Persist CDM JSON (encrypted) to object store.
        try:
            cdm_json = result.canonical_doc.model_dump_json().encode("utf-8")
            canonical_uri, canonical_iv = await self._ctx.objstore.put_canonical(
                doc_id, _INITIAL_CDM_VERSION, cdm_json, dek,
            )
        except Exception as e:
            await self._fail(
                tenant_id, doc_id, run_id,
                error_class="objstore_error", error_message=str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # Persist blocks + advance status + emit events in one transaction.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo  = DocumentRepo(conn)
            block_repo = CanonicalBlockRepo(conn)
            run_repo   = PipelineRunRepo(conn)
            timeline   = TimelineRepo(conn)
            outbox     = OutboxRepo(conn)

            inserted = await block_repo.bulk_insert(
                tenant_id, doc_id, self._ctx.pipeline.pipeline_version,
                result.canonical_doc.blocks,
            )

            await doc_repo.update_canonical(
                tenant_id, doc_id,
                canonical_uri=canonical_uri, canonical_iv=canonical_iv,
                language=result.canonical_doc.doc_metadata.language,
                page_count=result.canonical_doc.doc_metadata.page_count,
                duration_ms=result.canonical_doc.doc_metadata.duration_ms,
                title=result.canonical_doc.doc_metadata.title,
            )
            await doc_repo.update_status(tenant_id, doc_id, DocStatus.NORMALIZED)

            # Tier 3a upgrade — monotonic. Manual flag from /ingest/file (Tier 1)
            # already set 'sensitive' if user opted in; this is no-op then. If
            # the user marked 'standard' but our hard heuristics fire, we still
            # upgrade — heuristic match on IBAN/CC/etc. is unambiguous evidence
            # the user may not have realized was in the doc.
            if hard_hits:
                reason = ";".join(h.reason for h in hard_hits)
                await doc_repo.set_sensitivity_sensitive(tenant_id, doc_id, reason)
                log.info("extract.sensitivity_upgraded",
                         doc_id=str(doc_id), tier="3a",
                         reasons=[h.kind for h in hard_hits])

            await run_repo.succeed(run_id, metrics={
                "extractor": extractor.name,
                "extractor_version": extractor.extractor_version,
                "blocks": result.metrics.blocks,
                "pages": result.metrics.pages,
                "chars": result.metrics.chars,
                "duration_ms": duration_ms,
                "blocks_inserted": inserted,
                "tier3a_hits": [h.kind for h in hard_hits],
            })

            await timeline.record(
                tenant_id, "doc.indexed",                        # repurposed: structural milestone
                doc_id=doc_id, actor="system",
                payload={
                    "stage": self.name,
                    "extractor": extractor.name,
                    "blocks": result.metrics.blocks,
                },
            )

            await publish_doc_extracted(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                extractor=extractor.name,
                extractor_version=extractor.extractor_version,
                block_count=result.metrics.blocks,
                page_count=result.metrics.pages,
                causation_id=event.correlation_id,
            )
            await publish_doc_normalized(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                canonical_uri=canonical_uri, cdm_version=_INITIAL_CDM_VERSION,
                block_count=result.metrics.blocks,
                processing_profile=doc["processing_profile"],
                causation_id=event.correlation_id,
            )

        log.info("extract.normalized",
                 doc_id=str(doc_id),
                 extractor=extractor.name, blocks=result.metrics.blocks,
                 pages=result.metrics.pages, duration_ms=duration_ms)

    async def _fail(
        self, tenant_id: UUID, doc_id: UUID, run_id: UUID,
        error_class: str, error_message: str, retriable: bool,
        causation_id: UUID | None,
    ) -> None:
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_repo = PipelineRunRepo(conn)
            outbox   = OutboxRepo(conn)
            await run_repo.fail(run_id, error_class, error_message, retriable=retriable)
            if not retriable:
                # Mark doc as failed only on non-retriable errors; the publisher
                # retries transient ones via outbox attempts.
                await DocumentRepo(conn).mark_failed(tenant_id, doc_id)
            await publish_doc_failed(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                stage=self.name, error_class=error_class, error_message=error_message,
                retriable=retriable, causation_id=causation_id,
            )
        log.warning("extract.failed",
                    doc_id=str(doc_id), error_class=error_class,
                    error_message=error_message, retriable=retriable)
