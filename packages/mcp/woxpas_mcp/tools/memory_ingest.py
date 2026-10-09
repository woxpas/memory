"""memory_ingest — capture text content into memory.

For agents storing conversational memories, structured notes, or other
text content. Same downstream pipeline as /ingest/file (ingress → AV →
detect → CDM → summarize → chunk → embed → indexed).

Three-track scope model — Track 2 best practice:
  Agents calling from a conversation context SHOULD provide
  `suggested_scope_slugs`. If no matching scope exists, call
  memory_create_scope first, then ingest with the new slug.

The pipeline auto-assigns scope at ingest (lower threshold for
captured_by='agent' per Track 2a), and the dreamer retries scope match
later if no immediate match (Track 3a).

Returns 202-style: doc_id immediately. Pipeline runs async; agent can
poll memory_get_document_full to check status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import PreconditionFailed
from woxpas_mcp.registry import tool


class IngestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_text: str = Field(min_length=1, max_length=5_000_000)
    """The text to ingest. UTF-8. Max 5MB."""

    suggested_title: str | None = Field(default=None, max_length=500)
    suggested_processing_profile: str | None = Field(default=None, max_length=80)
    suggested_scope_slugs: list[str] = Field(default_factory=list)
    """Track 2 best practice: provide scope hints when calling from a
    conversation. Pipeline auto-assigns to matching scopes; agent may
    create new scopes via memory_create_scope first."""

    captured_by: Literal["user", "agent"] = "agent"
    """Default 'agent' since MCP callers are typically agents storing
    on behalf of the user. Override to 'user' for direct user-driven
    captures."""

    source_channel: Literal["api_custom", "mcp_inbound", "agent", "browser_extension"] = "mcp_inbound"
    """MCP-driven captures default to mcp_inbound."""

    sensitivity_class: Literal["standard", "sensitive"] | None = None
    valid_time_hint: datetime | None = None
    thread_hint: UUID | None = None

    on_duplicate: Literal["return_existing", "fail", "create_new"] = "return_existing"


@tool(
    name="memory_ingest",
    description=(
        "Save something to memory. Call this whenever the user shares "
        "new information worth remembering — a decision, a preference, "
        "a fact, a meeting note, a task.\n\n"
        "ALWAYS provide suggested_scope_slugs (e.g. ['work', 'project-x']) "
        "so the information is organized. If the scope doesn't exist yet, "
        "create it first with memory_create_scope.\n\n"
        "Returns immediately (status='received'). The pipeline processes "
"async — it'll be searchable within a few seconds.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=IngestInput,
)
async def memory_ingest(*, ctx: ToolContext, input: IngestInput) -> dict[str, Any]:

    from woxpas_adapters.base import IngestPayload
    from woxpas_core import CapturedBy, ContentRef, IngestEnvelope, SourceChannel
    from woxpas_pipeline.ingress import DuplicateDocument
    from woxpas_storage import SourceRepo

    content_bytes = input.content_text.encode("utf-8")

    async with ctx.pipeline_ctx.pool.tenant_session(ctx.tenant_id) as conn:
        source_id = await SourceRepo(conn).get_or_create(
            ctx.tenant_id, input.source_channel,
            f"default-{input.source_channel}",
            description=f"MCP text ingest via {input.source_channel}",
        )

    envelope = IngestEnvelope(
        tenant_id=ctx.tenant_id,
        source_id=source_id,
        source_channel=SourceChannel(input.source_channel),
        captured_by=CapturedBy(input.captured_by),
        capture_at=datetime.now(UTC),
        content_ref=ContentRef(uri="inline:"),
        mime="text/plain",
        suggested_title=input.suggested_title,
        suggested_processing_profile=input.suggested_processing_profile,
        suggested_scopes=list(input.suggested_scope_slugs),
        thread_hint=input.thread_hint,
        valid_time_hint=input.valid_time_hint,
        sensitivity_class=input.sensitivity_class,
    )
    payload = IngestPayload(envelope=envelope, content=content_bytes)

    try:
        doc_id, was_new = await ctx.ingress.submit(
            payload, on_duplicate=input.on_duplicate,
        )
    except DuplicateDocument as e:
        raise PreconditionFailed(
            f"duplicate document (doc_id={e.doc_id})", retriable=False,
        ) from e

    return {
        "doc_id":  str(doc_id),
        "status":  "received",
        "deduped": not was_new,
    }
