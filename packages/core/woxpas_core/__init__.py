"""Woxpas core type contracts.

Stable, shared types for every adapter, pipeline stage, and storage client.
Anything in here is part of the cross-package contract — break it intentionally,
not by accident. Bump pipeline_version and emit migration events when shapes change.
"""

from woxpas_core.canonical import Block, BlockType, CanonicalDoc, DocMetadata, StructureIndex
from woxpas_core.chunk import Chunk, Span
from woxpas_core.envelope import (
    CapturedBy,
    ContentRef,
    ExternalRef,
    IngestEnvelope,
    SourceChannel,
)
from woxpas_core.events import Event, EventEnvelope, EventType
from woxpas_core.ids import ContentHashable, content_uuid
from woxpas_core.locator import (
    AudioLocator,
    EmailLocator,
    ImageLocator,
    PdfLocator,
    SourceLocator,
    TextLocator,
    locator_kind,
)
from woxpas_core.profiles import AVResult, DocStatus, ProcessingProfile
from woxpas_core.versions import (
    ASR_MODEL_VERSION,
    EMBED_MODEL_VERSION,
    PIPELINE_VERSION,
    PROFILE_VERSIONS,
    SUMMARY_MODEL_VERSION,
)

__all__ = [
    "ASR_MODEL_VERSION",
    "EMBED_MODEL_VERSION",
    # versions
    "PIPELINE_VERSION",
    "PROFILE_VERSIONS",
    "SUMMARY_MODEL_VERSION",
    "AVResult",
    "AudioLocator",
    "Block",
    "BlockType",
    # canonical
    "CanonicalDoc",
    "CapturedBy",
    # chunk
    "Chunk",
    "ContentHashable",
    "ContentRef",
    "DocMetadata",
    "DocStatus",
    "EmailLocator",
    # events
    "Event",
    "EventEnvelope",
    "EventType",
    "ExternalRef",
    "ImageLocator",
    # envelope
    "IngestEnvelope",
    "PdfLocator",
    # profiles
    "ProcessingProfile",
    "SourceChannel",
    # locator
    "SourceLocator",
    "Span",
    "StructureIndex",
    "TextLocator",
    # ids
    "content_uuid",
    "locator_kind",
]
