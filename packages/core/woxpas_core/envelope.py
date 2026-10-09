"""IngestEnvelope — the unified entry point.

Every capture surface (file upload, voice note, MCP server, browser ext,
slack, agent, calendar, custom API) normalizes into this envelope before
hitting the pipeline. New surface = new adapter, zero pipeline change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceChannel(StrEnum):
    FILE_UPLOAD       = "file_upload"
    VOICE_NOTE        = "voice_note"
    MCP_INBOUND       = "mcp_inbound"
    MCP_OUTBOUND      = "mcp_outbound"
    BROWSER_EXTENSION = "browser_extension"
    SLACK             = "slack"
    EMAIL             = "email"
    AGENT             = "agent"
    CALENDAR          = "calendar"
    API_CUSTOM        = "api_custom"


class CapturedBy(StrEnum):
    USER   = "user"
    AGENT  = "agent"
    SYSTEM = "system"


class ContentRef(BaseModel):
    """How to fetch the content. Exactly one of {uri, bytes_b64, text} is set."""
    uri: str | None = None              # s3://, file://, https:// (only adapters fetch external)
    bytes_b64: str | None = None        # inline base64 — small payloads only
    text: str | None = None             # already-text payload (e.g., a thought)
    sha256_hex: str | None = None       # if the adapter pre-hashed; pipeline verifies

    @model_validator(mode="after")
    def _exactly_one(self) -> ContentRef:
        present = sum(x is not None for x in (self.uri, self.bytes_b64, self.text))
        if present != 1:
            raise ValueError("ContentRef requires exactly one of: uri | bytes_b64 | text")
        return self


class ExternalRef(BaseModel):
    """Round-trip identifier for two-way sync (calendar, slack thread, mcp source id)."""
    system: str                         # gcal | outlook | slack | notion | mcp:<server> | ...
    account_id: str | None = None
    object_id: str
    etag: str | None = None
    url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class IngestEnvelope(BaseModel):
    """Unified ingestion contract.

    Mandatory: tenant_id, source_id, source_channel, captured_by, capture_at, content_ref.
    Suggestions are optional; the pipeline's classifier will fill missing ones.
    """
    model_config = ConfigDict(frozen=False, extra="forbid")

    envelope_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    source_id: UUID
    source_channel: SourceChannel
    captured_by: CapturedBy = CapturedBy.USER

    capture_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    content_ref: ContentRef
    mime: str | None = None
    filename: str | None = None
    suggested_title: str | None = None

    suggested_processing_profile: str | None = None
    suggested_scopes: list[str] = Field(default_factory=list)   # slugs

    # User's manual sensitivity flag (Tier 1) — highest priority among the 3
    # tiers. None = use default 'standard'; downstream auto-tiers may upgrade
    # to 'sensitive'. Setting 'sensitive' here pre-empts any downgrade.
    sensitivity_class: str | None = None

    thread_hint: UUID | None = None
    valid_time_hint: datetime | None = None                     # when the content is "about", if known

    external_refs: list[ExternalRef] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    """Channel-specific blob: {url, page_title} for browser; {channel, ts, thread_ts} for slack;
    {server, tool_name} for mcp; {device, app} for voice note; etc."""

    # tracing
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None

    def fingerprint(self) -> str:
        """Stable string for logs / dedup hints (not a content hash)."""
        return f"{self.source_channel}:{self.source_id}:{self.envelope_id}"
