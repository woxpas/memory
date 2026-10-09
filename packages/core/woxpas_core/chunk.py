"""Chunks and spans — the retrieval unit and its UI-highlight granularity."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_core.ids import content_uuid
from woxpas_core.locator import SourceLocator


class Span(BaseModel):
    """Sentence-level (or smaller) unit within a chunk for UI highlighting.

    span_id is content-derived: hash of (chunk_id, ordinal, text, source_locator).
    Same chunk re-emitted → same span_ids → UI bookmarks survive pipeline upgrades.
    """
    model_config = ConfigDict(extra="forbid")

    span_id: UUID
    chunk_id: UUID
    doc_id: UUID
    block_id: UUID | None = None
    ordinal: int = Field(ge=0)
    text: str
    offset_in_chunk: tuple[int, int]    # (start, end) char offsets within chunk.text
    source_locator: SourceLocator
    pipeline_version: str

    @classmethod
    def make(
        cls,
        chunk_id: UUID,
        doc_id: UUID,
        ordinal: int,
        text: str,
        offset_in_chunk: tuple[int, int],
        source_locator: SourceLocator,
        pipeline_version: str,
        block_id: UUID | None = None,
    ) -> Span:
        sig = hashlib.sha256()
        sig.update(str(chunk_id).encode())
        sig.update(b"|")
        sig.update(str(ordinal).encode())
        sig.update(b"|")
        sig.update(text.encode("utf-8"))
        sig.update(b"|")
        sig.update(source_locator.model_dump_json().encode("utf-8"))
        span_id = content_uuid("span", sig.hexdigest())
        return cls(
            span_id=span_id,
            chunk_id=chunk_id,
            doc_id=doc_id,
            block_id=block_id,
            ordinal=ordinal,
            text=text,
            offset_in_chunk=offset_in_chunk,
            source_locator=source_locator,
            pipeline_version=pipeline_version,
        )


class Chunk(BaseModel):
    """Retrieval unit. Carries enough citation info to skip the original."""
    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    doc_id: UUID
    tenant_id: UUID
    ordinal: int = Field(ge=0)
    text: str
    token_count: int | None = None

    block_ids: list[UUID]
    source_locator: dict[str, Any]      # aggregate locator (e.g., {pages: [3,4], char_range: [..]})

    qdrant_point_id: UUID
    embed_model: str
    embed_model_version: str

    # mirrored to Qdrant payload for filter pushdown
    scope_ids: list[UUID] = Field(default_factory=list)
    processing_profile: str
    valid_time_start: datetime | None = None
    valid_time_end: datetime | None = None

    pipeline_version: str

    spans: list[Span] = Field(default_factory=list)

    @classmethod
    def derive_id(cls, doc_id: UUID, ordinal: int, text: str, model_version: str) -> UUID:
        """Compute a chunk's content-derived id.

        Includes model_version so re-embedding under a new model produces a fresh id
        (idempotent within a model, distinct across models).
        """
        sig = hashlib.sha256()
        sig.update(str(doc_id).encode())
        sig.update(b"|")
        sig.update(str(ordinal).encode())
        sig.update(b"|")
        sig.update(model_version.encode())
        sig.update(b"|")
        sig.update(text.encode("utf-8"))
        return content_uuid("chunk", sig.hexdigest())
