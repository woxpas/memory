"""File upload adapter.

Used by the API gateway when a user uploads a file via POST /ingest. Builds an
IngestEnvelope from the multipart form metadata and the raw bytes.

Validation is intentionally minimal here — defense lives at later stages (AV,
type detect, size limits in API config). The adapter just normalizes the shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from woxpas_core import (
    CapturedBy,
    ContentRef,
    ExternalRef,
    IngestEnvelope,
    SourceChannel,
)

from woxpas_adapters.base import Adapter, IngestPayload

log = structlog.get_logger(__name__)


class FileAdapter(Adapter):
    source_type = "file_upload"

    async def build_envelope(
        self,
        *,
        tenant_id: UUID,
        source_id: UUID,
        content: bytes,
        filename: str | None = None,
        mime_hint: str | None = None,
        suggested_title: str | None = None,
        suggested_processing_profile: str | None = None,
        suggested_scopes: list[str] | None = None,
        thread_hint: UUID | None = None,
        valid_time_hint: datetime | None = None,
        external_refs: list[ExternalRef] | None = None,
        captured_by: CapturedBy = CapturedBy.USER,
        source_metadata: dict[str, Any] | None = None,
        sensitivity_class: str | None = None,
    ) -> IngestPayload:
        envelope = IngestEnvelope(
            tenant_id=tenant_id,
            source_id=source_id,
            source_channel=SourceChannel.FILE_UPLOAD,
            captured_by=captured_by,
            capture_at=datetime.now(UTC),
            # Convention: uri="inline:" means content is in IngestPayload.content,
            # paired with this envelope. Ingress stage rewrites this to the s3:// URI
            # of the persisted blob before emitting ingest.received.
            content_ref=ContentRef(uri="inline:"),
            mime=mime_hint,
            filename=filename,
            suggested_title=suggested_title,
            suggested_processing_profile=suggested_processing_profile,
            suggested_scopes=list(suggested_scopes or []),
            thread_hint=thread_hint,
            valid_time_hint=valid_time_hint,
            external_refs=list(external_refs or []),
            source_metadata=source_metadata or {},
            sensitivity_class=sensitivity_class,
        )
        log.debug("file_adapter.envelope_built",
                  filename=filename, mime=mime_hint, size=len(content),
                  tenant_id=str(tenant_id))
        return IngestPayload(envelope=envelope, content=content)


__all__ = ["FileAdapter"]
