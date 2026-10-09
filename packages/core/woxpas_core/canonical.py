"""Canonical Document Model (CDM).

The lossless, structured intermediate representation. Persisted as versioned JSON
in the object store. Extraction reads the CDM forever, never the original PDF/DOCX.
Re-chunking, re-embedding, re-extracting all work off this artefact.

Block IDs are content-derived hashes so re-ingestion is idempotent. Extraction's
entity mentions, ideas, tasks all point to block_id (and char offsets within),
not to chunks. Chunks are a derivative — never the source of truth for downstream.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_core.ids import content_uuid
from woxpas_core.locator import SourceLocator


class BlockType(StrEnum):
    HEADING         = "heading"
    PARAGRAPH       = "paragraph"
    LIST_ITEM       = "list_item"
    TABLE           = "table"
    TABLE_ROW       = "table_row"
    CAPTION         = "caption"
    FOOTNOTE        = "footnote"
    QUOTE           = "quote"
    CODE            = "code"
    MATH            = "math"
    AUDIO_SEGMENT   = "audio_segment"
    IMAGE_CAPTION   = "image_caption"
    SIGNATURE_BLOCK = "signature_block"
    METADATA        = "metadata"


class Block(BaseModel):
    """A single addressable unit in the CDM.

    block_id is computed from (doc_id, ordinal, type, text|attrs, source_locator).
    Idempotent: same input → same id.
    """
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    ordinal: int = Field(ge=0)
    type: BlockType
    level: int | None = None
    parent_block_id: UUID | None = None

    text: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    source_locator: SourceLocator

    @classmethod
    def make(
        cls,
        doc_id: UUID,
        ordinal: int,
        type_: BlockType,
        source_locator: SourceLocator,
        text: str | None = None,
        attrs: dict[str, Any] | None = None,
        level: int | None = None,
        parent_block_id: UUID | None = None,
    ) -> Block:
        """Construct a Block with a content-derived block_id."""
        attrs = attrs or {}
        sig = hashlib.sha256()
        sig.update(str(doc_id).encode())
        sig.update(b"|")
        sig.update(str(ordinal).encode())
        sig.update(b"|")
        sig.update(type_.value.encode())
        sig.update(b"|")
        sig.update((text or "").encode("utf-8"))
        sig.update(b"|")
        sig.update(source_locator.model_dump_json(by_alias=False).encode("utf-8"))
        block_id = content_uuid("block", sig.hexdigest())
        return cls(
            block_id=block_id,
            ordinal=ordinal,
            type=type_,
            level=level,
            parent_block_id=parent_block_id,
            text=text,
            attrs=attrs,
            source_locator=source_locator,
        )


class StructureIndex(BaseModel):
    """Pre-computed structural shortcuts so consumers don't re-walk the block graph."""
    headings_tree: list[dict[str, Any]] = Field(default_factory=list)
    table_of_contents: list[dict[str, Any]] = Field(default_factory=list)
    footnote_refs: list[dict[str, Any]] = Field(default_factory=list)
    cross_refs: list[dict[str, Any]] = Field(default_factory=list)


class DocMetadata(BaseModel):
    """Origin metadata pulled from the source format. Cheap, high-leverage for extraction."""
    title: str | None = None
    author: str | None = None
    authors: list[str] = Field(default_factory=list)
    created_at_in_doc: datetime | None = None
    modified_at_in_doc: datetime | None = None
    language: str | None = None
    page_count: int | None = None
    duration_ms: int | None = None
    source_format: str | None = None
    encoding: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CanonicalDoc(BaseModel):
    """The persisted CDM artefact. Versioned in the object store."""
    model_config = ConfigDict(extra="forbid")

    doc_id: UUID
    tenant_id: UUID
    pipeline_version: str
    schema_version: str = "1"

    blocks: list[Block]
    doc_metadata: DocMetadata
    structure_index: StructureIndex = Field(default_factory=StructureIndex)

    extractor: str
    extractor_version: str
    extracted_at: datetime
