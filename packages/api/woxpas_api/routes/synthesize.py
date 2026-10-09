"""POST /ask + POST /synthesize — SSE-streamed citation-bound synthesis.

Two endpoints, one engine, two defaults:

  POST /ask        → lean envelope (`include_chunk_text=False`). Default for
                     agents — Citation carries identity + attribution only;
                     full chunk text fetched on demand via GET /chunks/<id>.
                     This is the cheap path for token-limited agents.

  POST /synthesize → heavy envelope (`include_chunk_text=True` by default).
                     For UI surfaces that render source text inline. Caller
                     can still override with `include_chunk_text` in the body.

Both stream Server-Sent Events. The wire format:
    event: <kind>\\n
    data: <json>\\n\\n

Event kinds (in order, per SynthesisEvent):
  sources       — retrieval result preview + citation_map
  delta         — incremental answer text (zero or more)
  continuation  — emitted when a continuation LLM call starts (4.5a)
  done          — LLM stream complete; usage + finish_reason
  answer        — parsed SynthesizedAnswer (claims + citations)
  verify        — per-citation verification status (4.4)
  error         — synthesis failed; stream terminates after this

Edge-case handling baked in:
  - Disconnect detection: poll request.is_disconnected() between events;
    cancel upstream on disconnect (don't burn cloud tokens for an absent
    client).
  - Buffer-bypass headers: Cache-Control: no-cache, X-Accel-Buffering: no
    so Nginx + CDNs don't accumulate the stream.
  - Keepalive: when no event for 15s (long thinking on reasoning models,
    embedder warmup), emit `: keepalive\\n\\n` SSE comments to keep
    proxies + browsers from killing the connection.
  - Pydantic validation: max bounds on every numeric/list field, lengths
    on text, list cap on pinned_chunk_ids.
  - UUID JSON serialization: handled by _to_jsonable + Pydantic mode='json'.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from woxpas_pipeline.cloud_policy import CloudPolicyDenied
from woxpas_synthesis import (
    ErrorEvent,
    SynthesisEvent,
    SynthesisRequest,
)

from woxpas_api.auth import tenant_id_dep
from woxpas_api.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(tags=["synthesis"])


# Keepalive cadence — long enough that healthy connections don't see
# unnecessary chatter, short enough to beat default proxy idle timeouts
# (typically 30-60s).
_KEEPALIVE_INTERVAL_S = 15.0


# ─── Pydantic body — wire-protocol contract ────────────────────────────────

class SynthesisBody(BaseModel):
    """Wire-protocol body for /ask and /synthesize.

    Every numeric and list bound is enforced here so a malformed agent
    request fails fast with a 422 before the engine sees it. Bounds are
    deliberately permissive (we don't want to be cute), but they protect
    against denial-of-service via giant pinned_chunk_ids arrays or
    oversized persona blobs.
    """
    model_config = ConfigDict(extra="ignore")

    query_text:        str = Field(min_length=1, max_length=4000)
    """The user's question. Required, capped to keep prompt sizes sane."""

    intent_hint:       str | None = Field(default=None, max_length=32)
    """Retrieval intent hint ('semantic', 'timeline', etc). None = AUTO."""

    max_sources:       int = Field(default=8, ge=1, le=20)
    """How many top-N chunks to send to the LLM."""
    rerank:            bool = True

    # Agent-continuity (4.4a)
    pinned_chunk_ids:  list[UUID] = Field(default_factory=list, max_length=50)
    """Chunk_ids from a prior turn. Cross-tenant ids are silently dropped."""
    pinned_only:       bool = False
    include_chunk_text: bool | None = None
    """None means use the endpoint's default: False for /ask, True for /synthesize."""

    # LLM
    model_name:        str | None = Field(default=None, max_length=80)
    max_tokens:        int = Field(default=1024, ge=16, le=8192)
    temperature:       float = Field(default=0.0, ge=0.0, le=2.0)
    persona:           str | None = Field(default=None, max_length=500)
    timeout_s:         float = Field(default=120.0, ge=1.0, le=600.0)

    # Cloud-policy (4.5)
    allow_cloud_for_personal_data:  bool | None = None
    allow_cloud_for_sensitive_data: bool | None = None

    def to_engine_request(self) -> SynthesisRequest:
        """Convert wire-protocol body to engine dataclass."""
        return SynthesisRequest(
            query_text=self.query_text,
            intent_hint=self.intent_hint,
            max_sources=self.max_sources,
            rerank=self.rerank,
            pinned_chunk_ids=list(self.pinned_chunk_ids),
            pinned_only=self.pinned_only,
            include_chunk_text=bool(self.include_chunk_text),
            model_name=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            persona=self.persona,
            timeout_s=self.timeout_s,
            allow_cloud_for_personal_data=self.allow_cloud_for_personal_data,
            allow_cloud_for_sensitive_data=self.allow_cloud_for_sensitive_data,
        )


# ─── Routes ────────────────────────────────────────────────────────────────

def _get_state(request: Request) -> AppState:
    return request.app.state.app


@router.post("/ask")
async def ask(
    body:      SynthesisBody,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> StreamingResponse:
    """Agent-friendly synthesis. Defaults to LEAN citation envelope
    (no chunk_text in citations) — agents fetch full chunk text on demand
    via GET /chunks/<chunk_id>. Same engine + same SSE protocol as
    /synthesize."""
    if body.include_chunk_text is None:
        body.include_chunk_text = False
    return _stream_synthesis(body, request, tenant_id)


@router.post("/synthesize")
async def synthesize(
    body:      SynthesisBody,
    request:   Request,
    tenant_id: Annotated[UUID, Depends(tenant_id_dep)],
) -> StreamingResponse:
    """Structured synthesis for UI / research surfaces. Defaults to HEAVY
    citation envelope (chunk_text inline) — caller renders source text
    alongside the answer without separate fetches. Caller can override
    via body.include_chunk_text=False."""
    if body.include_chunk_text is None:
        body.include_chunk_text = True
    return _stream_synthesis(body, request, tenant_id)


# ─── SSE plumbing ──────────────────────────────────────────────────────────

def _stream_synthesis(
    body: SynthesisBody, request: Request, tenant_id: UUID,
) -> StreamingResponse:
    """Build the SSE response. Headers chosen to defeat common buffering
    pitfalls (Nginx, CDNs, browser proxies).
    """
    state = _get_state(request)
    engine_req = body.to_engine_request()
    return StreamingResponse(
        _sse_generator(state, engine_req, tenant_id, request),
        media_type="text/event-stream",
        headers={
            # Tell intermediaries not to buffer this response.
            "Cache-Control":     "no-cache, no-transform",
            "X-Accel-Buffering": "no",        # Nginx
            "Connection":        "keep-alive",
        },
    )


async def _sse_generator(
    state, engine_req: SynthesisRequest, tenant_id: UUID, request: Request,
) -> AsyncIterator[bytes]:
    """Bridge SynthesisEvent stream → SSE wire format with disconnect
    detection + keepalive.

    Architecture: a producer task drains the engine's async generator into
    a queue. The main loop pulls from the queue with a timeout that, when
    hit, emits a keepalive SSE comment. On client disconnect, the producer
    task is cancelled — which propagates to the LLM stream and stops the
    upstream cloud egress (saving tokens).
    """
    request_id = str(engine_req.request_id)
    t_start = time.monotonic()

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    _DONE = object()
    _ERROR = object()

    async def _producer() -> None:
        try:
            async for ev in state.synthesis.synthesize_stream(
                engine_req, tenant_id=tenant_id,
            ):
                await queue.put(("event", ev))
        except asyncio.CancelledError:
            # Propagated from disconnect — exit silently.
            raise
        except Exception as e:
            # Full detail stays server-side; the client sees a generic
            # message for anything that isn't a known domain error.
            log.error("synthesize.stream.failed",
                      request_id=request_id, tenant_id=str(tenant_id),
                      error=str(e), error_class=type(e).__name__)
            await queue.put(("error", e))
        await queue.put(("done", None))

    producer_task = asyncio.create_task(_producer())
    log.info("synthesize.stream.begin",
             request_id=request_id, tenant_id=str(tenant_id),
             query_preview=engine_req.query_text[:80])

    try:
        while True:
            # Disconnect check BEFORE pulling next event — saves us emitting
            # a delta into the void if the client just closed.
            if await request.is_disconnected():
                log.info("synthesize.stream.client_disconnected",
                         request_id=request_id)
                producer_task.cancel()
                break

            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=_KEEPALIVE_INTERVAL_S,
                )
            except TimeoutError:
                # No event in keepalive window → emit comment + loop.
                # SSE comments are lines starting with `:` and are ignored
                # by clients. Proxies/browsers see traffic and don't time
                # out the connection.
                yield b": keepalive\n\n"
                continue

            if kind == "done":
                break
            if kind == "error":
                err = payload
                # Map exception → ErrorEvent so the client always sees a
                # structured terminator. CloudPolicyDenied is non-retriable.
                # Only surface the message for known, safe domain errors
                # (e.g. a policy denial the agent must act on). For anything
                # unexpected we return a generic message — the full detail is
                # already logged server-side and must not leak to the client.
                is_domain_error = isinstance(err, CloudPolicyDenied)
                error_event = ErrorEvent(
                    error_class=type(err).__name__,
                    error_message=(
                        str(err)[:300] if is_domain_error
                        else "synthesis failed; see server logs"
                    ),
                    retriable=not isinstance(err, CloudPolicyDenied),
                )
                yield _encode_event(error_event)
                break

            yield _encode_event(payload)
    finally:
        # Always cancel + drain producer on exit. If we're here because
        # of disconnect, this is what cuts the upstream cloud-LLM stream.
        if not producer_task.done():
            producer_task.cancel()
        try:
            await producer_task
        except (asyncio.CancelledError, Exception):  # noqa: S110  # best-effort await of a cancelled producer during SSE teardown; any real error already reached the stream
            pass
        log.info("synthesize.stream.end",
                 request_id=request_id,
                 duration_ms=round((time.monotonic() - t_start) * 1000, 1))


# ─── Event encoding ────────────────────────────────────────────────────────

def _encode_event(event: SynthesisEvent) -> bytes:
    """Convert a SynthesisEvent to SSE wire format.

    Each event becomes `event: <kind>\\n` + `data: <json>\\n\\n`. The
    `kind` field on each event is the SSE channel name; clients dispatch
    by channel. UUIDs and datetimes are converted to strings. Dataclass
    nesting (e.g., AnswerEvent.answer is a SynthesizedAnswer dataclass
    with nested Citations) is recursively unwrapped.
    """
    kind = event.kind
    payload = _to_jsonable(event)
    payload.pop("kind", None)   # don't double-emit the discriminator
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {kind}\ndata: {body}\n\n".encode()


def _to_jsonable(value: Any) -> Any:
    """Recursive conversion to JSON-serializable Python objects.

    Handles UUIDs (→ str), datetimes (→ ISO 8601), dataclasses (→ dict),
    sets (→ list), and falls through to default types. Used everywhere
    we serialize an engine event to the wire."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, set):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    # Fallback — Pydantic models, SQLAlchemy rows, etc.
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump(mode="json"))
    return str(value)
