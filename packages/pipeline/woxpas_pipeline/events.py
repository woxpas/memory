"""Typed event publish helpers.

Wraps OutboxRepo with one function per event type. Callers don't construct
Event() objects manually — they call the right `publish_*` and the helper
fills in event_type, aggregate_kind, version stamps, and payload.

Always called inside the same SQL transaction as the state change that triggered
the event (transactional outbox).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from woxpas_core import Event, EventType
from woxpas_storage import OutboxRepo


async def publish_ingest_received(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    source_channel: str, sha256_hex: str, mime: str | None,
    correlation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.INGEST_RECEIVED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        payload={
            "source_channel": source_channel,
            "sha256_hex": sha256_hex,
            "mime": mime,
        },
    ))


async def publish_doc_scanned(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    av_result: str, av_signature: str | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_SCANNED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={"av_result": av_result, "av_signature": av_signature},
    ))


async def publish_doc_detected(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    mime: str, profile_suggested: str | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_DETECTED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={"mime": mime, "profile_suggested": profile_suggested},
    ))


async def publish_doc_extracted(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    extractor: str, extractor_version: str, block_count: int, page_count: int,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_EXTRACTED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={
            "extractor": extractor, "extractor_version": extractor_version,
            "block_count": block_count, "page_count": page_count,
        },
    ))


async def publish_doc_normalized(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    canonical_uri: str, cdm_version: int,
    block_count: int, processing_profile: str,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_NORMALIZED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={
            "canonical_uri": canonical_uri,
            "cdm_version": cdm_version,
            "block_count": block_count,
            "processing_profile": processing_profile,
        },
    ))


async def publish_doc_summarized(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    summary_preview: str, topics: list[str],
    model_version: str, profile_version: str,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_SUMMARIZED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={
            "summary_preview": summary_preview[:240],
            "topics": topics,
            "model_version": model_version,
            "profile_version": profile_version,
        },
    ))


async def publish_doc_indexed(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    chunk_count: int, embed_model: str, embed_model_version: str,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_INDEXED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={
            "chunk_count": chunk_count,
            "embed_model": embed_model,
            "embed_model_version": embed_model_version,
        },
    ))


async def publish_doc_extracted_facts(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    chunks_processed: int, chunks_failed: int,
    entities_count: int, ideas_count: int,
    tasks_count: int, claims_count: int,
    extractor_version: str,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_EXTRACTED_FACTS,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={
            "chunks_processed":  chunks_processed,
            "chunks_failed":     chunks_failed,
            "entities_count":    entities_count,
            "ideas_count":       ideas_count,
            "tasks_count":       tasks_count,
            "claims_count":      claims_count,
            "extractor_version": extractor_version,
        },
    ))


async def publish_doc_linked(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    predicates_normalized: int, predicates_unmatched: int,
    entities_merged: int, profile_facts_added: int,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> int:
    return await outbox.publish(Event(
        event_type=EventType.DOC_LINKED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload={
            "predicates_normalized": predicates_normalized,
            "predicates_unmatched":  predicates_unmatched,
            "entities_merged":       entities_merged,
            "profile_facts_added":   profile_facts_added,
        },
    ))


async def publish_doc_failed(
    outbox: OutboxRepo, *,
    tenant_id: UUID, doc_id: UUID, pipeline_version: str,
    stage: str, error_class: str, error_message: str, retriable: bool,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    payload = {
        "stage": stage,
        "error_class": error_class,
        "error_message": error_message,
        "retriable": retriable,
    }
    if extra:
        payload.update(extra)
    return await outbox.publish(Event(
        event_type=EventType.DOC_FAILED,
        pipeline_version=pipeline_version,
        aggregate_kind="document", aggregate_id=doc_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id or uuid4(),
        causation_id=causation_id,
        payload=payload,
    ))
