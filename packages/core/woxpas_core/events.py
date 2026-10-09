"""Pipeline events — the contracts that decouple ingestion from extraction,
retrieval, and surfacing.

Events are forward-compatible: only additive payload changes. Consumers pin a
version range. The transactional outbox guarantees exactly-once-effectively
delivery; pipelines write events in the same SQL transaction as state changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    # Ingestion lifecycle
    INGEST_RECEIVED                = "ingest.received"
    DOC_SCANNED                    = "doc.scanned"
    DOC_DETECTED                   = "doc.detected"
    DOC_EXTRACTED                  = "doc.extracted"
    DOC_NORMALIZED                 = "doc.normalized"
    DOC_SUMMARIZED                 = "doc.summarized"
    DOC_CHUNKED                    = "doc.chunked"
    DOC_EMBEDDED                   = "doc.embedded"
    DOC_INDEXED                    = "doc.indexed"
    DOC_FAILED                     = "doc.failed"
    DOC_SUPERSESSION_CANDIDATE     = "doc.supersession_candidate"

    # Cross-pipeline (consumed by extraction / surfacing — emitted later)
    DOC_EXTRACTED_FACTS            = "doc.extracted_facts"
    DOC_LINKED                     = "doc.linked"
    ENTITY_CREATED                 = "entity.created"
    ENTITY_MERGED                  = "entity.merged"
    IDEA_CREATED                   = "idea.created"
    TASK_CREATED                   = "task.created"
    CONFLICT_DETECTED              = "conflict.detected"


class Event(BaseModel):
    """Base event payload. Concrete events extend this with `payload` shapes."""
    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    event_version: str = "1"
    pipeline_version: str

    aggregate_kind: str                        # document | entity | idea | task | conflict
    aggregate_id: UUID
    tenant_id: UUID

    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None

    payload: dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(BaseModel):
    """Outbox row representation."""
    model_config = ConfigDict(extra="forbid")

    outbox_id: int
    event: Event
    scheduled_for: datetime
    available_after: datetime
    attempts: int = 0
    max_attempts: int = 8
    published_at: datetime | None = None
    failed_at: datetime | None = None
    last_error: str | None = None
