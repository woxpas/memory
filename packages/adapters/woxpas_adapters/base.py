"""Adapter Protocol — every capture surface implements this.

The contract is intentionally narrow: an adapter takes channel-specific input
and produces an IngestEnvelope + raw content bytes. The pipeline takes it from
there. Adapters never write to Postgres, Qdrant, or the object store directly.

Adding a new capture surface = a new module that implements `build_envelope`.
Zero pipeline changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from woxpas_core import IngestEnvelope


@dataclass(slots=True)
class IngestPayload:
    """An envelope plus the raw bytes it points to.

    Most adapters yield (envelope, content) atomically. Streaming adapters
    (large files, long audio) can subclass to expose an async iterator instead;
    the pipeline's ingress stage will handle either shape via overload.
    """
    envelope: IngestEnvelope
    content: bytes


@runtime_checkable
class Adapter(Protocol):
    """Channel-specific producer of ingest payloads."""

    source_type: str

    async def build_envelope(self, **kwargs: object) -> IngestPayload:
        """Construct an envelope + payload from channel-specific arguments.

        kwargs vary per adapter; adapters declare their own typed wrappers
        on top of this Protocol method.
        """
        ...
