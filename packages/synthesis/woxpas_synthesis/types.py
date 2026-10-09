"""Synthesis request/event types.

The SynthesisEngine takes a SynthesisRequest and yields a stream of
SynthesisEvents. The events map cleanly to the SSE event sequence that
the API layer (Phase 4.6) will surface to clients.

Event timeline:
  SourcesEvent   — retrieval completed; here are the sources we'll cite
  DeltaEvent     — incremental text from the LLM (zero or more)
  DoneEvent      — LLM stream finished; final usage + finish_reason
  AnswerEvent    — post-parse SynthesizedAnswer (citations + claims,
                   pre-verification)
  VerifyEvent    — added in Phase 4.4 once verifier runs; carries
                   per-citation status updates
  ErrorEvent     — anything went wrong

Why not just yield strings: the API layer needs to emit DIFFERENT
SSE event names per concern. Using a typed event union keeps the
engine pure and the event encoder small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

from woxpas_runtime import TokenUsage

from woxpas_synthesis.schema import (
    CitationSource,
    SynthesizedAnswer,
)

# ─── Request ────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class SynthesisRequest:
    """Single-entry synthesis request.

    tenant_id is derived from auth at the API boundary — never trusted from
    request body (cloud-portability + tenant-isolation requirement, mirrors
    RetrievalRequest).

    `model_name` overrides the default synthesis model. None means use the
    factory's default. Caller verifies via `is_cloud` and the cloud-policy
    gate (Phase 4.5) before egress.

    `persona` is a task-specific style modifier passed through to the
    prompt ("Be concise", "Use a friendly tone").

    `intent_hint` is the agent's hint for retrieval; AUTO falls through to
    the rule classifier. We don't restrict to SEMANTIC because the agent
    might know better — but the engine forces a fallback to SEMANTIC if
    the resolved intent doesn't return chunk-shaped items, so synthesis
    always has chunks to cite.

    `max_sources` is the hard cap on chunks sent to the LLM. Reranker
    output is sliced to top-N before building citation_map. Higher = more
    grounding, more tokens, slower stream.

    Agent continuity (lean envelope + pinned chunks):
      `pinned_chunk_ids` — chunk_ids from a prior synthesis turn. Engine
        fetches these (tenant-scoped) and PRE-PENDS to the citation_map,
        preserving stable numeric ids 1..N for follow-up consistency.
        New chunks from retrieval get ids N+1, N+2, ...
      `pinned_only` — when True, skip retrieval entirely; only the pinned
        chunks form the citation_map. Use for "drill down on citation 3"
        / "elaborate on these specific sources" follow-up turns. Saves
        the retrieval pass (no embedder + reranker latency).
      `include_chunk_text` — when True, response Citation objects carry
        the full chunk text. Default False (lean envelope; agents fetch
        full text on demand via GET /chunks/<id>). Set True only when
        the caller will render source text inline (UI surfaces, etc.).
    """
    request_id:     UUID = field(default_factory=uuid4)
    query_text:     str  = ""

    # Retrieval shaping
    intent_hint:    str | None = None       # "semantic" / "timeline" / etc; None=AUTO
    max_sources:    int  = 8                # top-N chunks sent to the LLM
    rerank:         bool = True

    # Agent-continuity knobs (Phase 4.4a)
    pinned_chunk_ids:   list[UUID] = field(default_factory=list)
    pinned_only:        bool = False
    include_chunk_text: bool = False

    # LLM shaping
    model_name:     str | None = None       # override default synthesis model
    max_tokens:     int  = 1024
    temperature:    float = 0.0
    persona:        str | None = None
    timeout_s:      float = 120.0

    # Cloud-policy (Phase 4.5)
    # Semantics: AND across BOTH tenant policy and request flag.
    #   None  — use tenant policy as-is (default, recommended)
    #   True  — request asks for cloud (still requires tenant=True; can't
    #           grant beyond tenant ceiling)
    #   False — request explicitly denies cloud for THIS call regardless
    #           of tenant policy (deny-only override)
    # Default None means "respect tenant settings" — opt-in is at tenant
    # level, not per-request, so users who configured their tenant for
    # cloud don't have to re-enable on every call.
    allow_cloud_for_personal_data:  bool | None = None
    allow_cloud_for_sensitive_data: bool | None = None


# ─── Events emitted by the engine ───────────────────────────────────────────
#
# All events are dataclasses with a `kind` discriminator string. The API
# layer in 4.6 will encode each kind as a separate SSE `event:` channel.

@dataclass(slots=True)
class SourcesEvent:
    """Retrieval result preview, emitted before the LLM stream starts.

    The UI can render "Found 5 sources, generating answer…" while the
    user waits for the LLM's first token. This is also where the citation
    chunk_ids → integer ids mapping is established."""
    kind: Literal["sources"] = "sources"
    sources:        list[CitationSource] = field(default_factory=list)
    chunks_total:   int = 0                       # total chunks retrieval saw
    chunks_used:    int = 0                       # how many we sliced for the LLM
    retrieval_intent: str | None = None           # which intent was resolved


@dataclass(slots=True)
class DeltaEvent:
    """Incremental text chunk from the LLM stream."""
    kind: Literal["delta"] = "delta"
    text: str = ""


@dataclass(slots=True)
class DoneEvent:
    """LLM stream completed (finish_reason + usage). Emitted BEFORE
    AnswerEvent so the UI can stop the streaming spinner immediately,
    while the parser runs.

    `usage` aggregates ALL LLM calls including continuations — so if the
    answer required 2 continuation calls, usage.total_tokens reflects the
    sum across all 3 calls. This is what an operator wants for cost
    accounting."""
    kind: Literal["done"] = "done"
    finish_reason:      str = "stop"
    usage:              TokenUsage = field(default_factory=TokenUsage)
    continuation_count: int = 0           # number of continuation LLM calls (0 = one-shot)


@dataclass(slots=True)
class ContinuationEvent:
    """Emitted between LLM calls when the previous call truncated and a
    continuation pass is starting. Optional UI signal — clients can
    ignore. Useful for debug overlays / progress indicators."""
    kind: Literal["continuation"] = "continuation"
    iteration:           int = 1          # 1 = first continuation, 2 = second, ...
    partial_chars:       int = 0          # how much we've accumulated so far
    reason:              str = "max_tokens"


@dataclass(slots=True)
class AnswerEvent:
    """Parsed SynthesizedAnswer with claims + citations.

    Citations are still in `unverified` / `fabricated` status here — the
    Phase 4.4 verifier upgrades them to `verified` / `weak` / `unverified`
    via a separate VerifyEvent."""
    kind: Literal["answer"] = "answer"
    answer: SynthesizedAnswer | None = None


@dataclass(slots=True)
class VerifyEvent:
    """Per-citation verification result. Emitted by Phase 4.4 verifier
    after AnswerEvent — independent stream so verification latency
    doesn't block the user-visible answer.

    Updates a citation's status, cosine, and reason. The client UI
    re-renders citation markers based on this."""
    kind: Literal["verify"] = "verify"
    citation_updates: dict[int, dict[str, Any]] = field(default_factory=dict)
    """citation_id → {status, cosine, reason}"""


@dataclass(slots=True)
class ErrorEvent:
    """Synthesis failed. The stream terminates after this event."""
    kind: Literal["error"] = "error"
    error_class: str = ""
    error_message: str = ""
    retriable: bool = False


# Discriminated-union type the engine yields.
SynthesisEvent = (
    SourcesEvent | DeltaEvent | ContinuationEvent | DoneEvent
    | AnswerEvent | VerifyEvent | ErrorEvent
)
