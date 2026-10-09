"""Extractor base + result type.

An extractor takes raw bytes + a hint about the doc and produces a
CanonicalDoc. Pure function — no I/O, no DB, no model state. The Extract stage
wraps extractors with persistence and event emission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from woxpas_core import CanonicalDoc, DocMetadata


@dataclass(slots=True)
class ExtractInput:
    """All the context an extractor might want."""
    doc_id:           UUID
    tenant_id:        UUID
    pipeline_version: str
    content:          bytes
    mime:             str
    filename:         str | None = None
    declared_title:   str | None = None
    declared_language: str | None = None


@dataclass(slots=True)
class ExtractMetrics:
    """Operational metrics for pipeline_runs.metrics."""
    blocks:        int = 0
    pages:         int = 0
    chars:         int = 0
    duration_ms:   int | None = None
    extra:         dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractResult:
    canonical_doc: CanonicalDoc
    metrics:       ExtractMetrics


@runtime_checkable
class Extractor(Protocol):
    """Implementations live in extract/{text,pdf,docx,...}.py and register themselves
    with the registry. The stage looks them up by MIME at dispatch time.
    """

    name: str
    extractor_version: str
    supported_mimes: tuple[str, ...]

    def extract(self, inp: ExtractInput) -> ExtractResult: ...


def empty_metadata() -> DocMetadata:
    return DocMetadata()


def now_iso() -> datetime:
    return datetime.now()
