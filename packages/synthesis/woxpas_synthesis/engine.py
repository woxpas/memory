"""SynthesisEngine — orchestrates retrieval → prompt → stream → parse.

Construction:
  Build once at server / CLI startup. Reuse across requests.

Public surface:
  synthesize_stream(request, *, tenant_id) -> AsyncIterator[SynthesisEvent]
  synthesize        (request, *, tenant_id) -> SynthesizedAnswer

The streaming method is the canonical path — it yields events that map
1:1 to SSE channels. The non-streaming method is a convenience that
drains the stream and returns the final SynthesizedAnswer (used by the
CLI and integration tests).

Event sequence per successful call:
  1. SourcesEvent      — chunks retrieved + citation_map established
  2. DeltaEvent[]      — token chunks streaming from the LLM
  3. DoneEvent         — LLM stream finished (usage, finish_reason)
  4. AnswerEvent       — parsed SynthesizedAnswer (citations unverified)
  5. (Phase 4.4) VerifyEvent — citation verification status

Failures emit a single ErrorEvent and end the stream.

Cloud-policy gating: the engine calls assert_cloud_allowed_for_synthesis
pre-flight over the bag of retrieved chunks (worst-case sensitivity +
profile). A cloud backend is REFUSED with CloudPolicyDenied when the
chunks exceed tenant policy; local backends always pass. Every egress —
allowed, denied, or errored — is written to audit_log.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

if TYPE_CHECKING:
    from woxpas_storage import PostgresPool

# CloudPolicyDenied is the only exception we WANT to surface as an
# ErrorEvent without retry guidance — re-import for isinstance check.
from woxpas_pipeline.cloud_policy import CloudPolicyDenied
from woxpas_retrieval import (
    RetrievalEngine,
    RetrievalRequest,
)
from woxpas_retrieval.types import Intent, ResultItem
from woxpas_runtime import LlmBackend, Message, TokenUsage

from woxpas_synthesis.cloud_policy import (
    assert_cloud_allowed_for_synthesis,
    audit_synthesis_cloud_egress,
)
from woxpas_synthesis.parser import parse as parse_answer
from woxpas_synthesis.prompt import build_messages
from woxpas_synthesis.schema import CitationSource, SynthesizedAnswer
from woxpas_synthesis.types import (
    AnswerEvent,
    ContinuationEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    SourcesEvent,
    SynthesisEvent,
    SynthesisRequest,
    VerifyEvent,
)
from woxpas_synthesis.verifier import CitationVerifier

log = structlog.get_logger(__name__)


# Type alias for the LLM factory — caller wires up their cache-aware
# build function (the API has one; the CLI has a simpler one).
LlmFactory = Callable[[str | None], LlmBackend]


class SynthesisEngine:
    """Synthesis orchestrator. Stateless across requests apart from
    held-handle dependencies."""

    def __init__(
        self,
        *,
        retrieval:   RetrievalEngine,
        llm_factory: LlmFactory,
        pool:        PostgresPool,
        verifier:    CitationVerifier | None = None,
    ) -> None:
        self._retrieval   = retrieval
        self._llm_factory = llm_factory
        self._pool        = pool                  # for cloud-policy + audit
        self._verifier    = verifier

    # ─── Streaming entry point ──────────────────────────────────────────────

    async def synthesize_stream(
        self,
        request: SynthesisRequest,
        *,
        tenant_id: UUID,
    ) -> AsyncIterator[SynthesisEvent]:
        """Run synthesis end-to-end and yield events.

        Errors are caught and surfaced as a single ErrorEvent before the
        generator exits. Callers iterating with `async for` will see the
        error and stop — no exception leaks out (intentional, mirrors how
        SSE streams must terminate gracefully on the wire)."""
        t_start = time.monotonic()
        log.info("synthesis.begin",
                 request_id=str(request.request_id),
                 tenant_id=str(tenant_id),
                 query_preview=request.query_text[:80])
        try:
            async for ev in self._run(request, tenant_id=tenant_id):
                yield ev
        except Exception as e:
            log.error("synthesis.failed",
                      request_id=str(request.request_id),
                      error_class=type(e).__name__,
                      error=str(e)[:300])
            yield ErrorEvent(
                error_class=type(e).__name__,
                error_message=str(e)[:300],
                retriable=_is_retriable(e),
            )
        finally:
            log.info("synthesis.end",
                     request_id=str(request.request_id),
                     duration_ms=round((time.monotonic() - t_start) * 1000, 1))

    # ─── Non-streaming convenience ─────────────────────────────────────────

    async def synthesize(
        self,
        request: SynthesisRequest,
        *,
        tenant_id: UUID,
    ) -> SynthesizedAnswer:
        """Drain the stream, return the final SynthesizedAnswer.

        Used by the CLI and tests where streaming UX isn't needed. Raises
        on error (no graceful ErrorEvent — the caller wanted the answer,
        not a placeholder)."""
        answer: SynthesizedAnswer | None = None
        error: ErrorEvent | None = None
        async for ev in self.synthesize_stream(request, tenant_id=tenant_id):
            if isinstance(ev, AnswerEvent):
                answer = ev.answer
            elif isinstance(ev, ErrorEvent):
                error = ev
        if error is not None:
            raise RuntimeError(
                f"synthesis failed [{error.error_class}]: {error.error_message}"
            )
        if answer is None:
            raise RuntimeError("synthesis ended with no AnswerEvent")
        return answer

    # ─── Private orchestrator ──────────────────────────────────────────────

    async def _run(
        self,
        request: SynthesisRequest,
        *,
        tenant_id: UUID,
    ) -> AsyncIterator[SynthesisEvent]:
        # ─── 1. Resolve sources: pinned + retrieved ────────────────────────
        t0 = time.monotonic()

        # Pinned chunks come first — they preserve stable numeric ids
        # across follow-up turns ("citation 3" means the same chunk turn
        # to turn). Tenant-scoped fetch; cross-tenant ids are silently
        # dropped at the SQL boundary.
        pinned_chunks: list[ResultItem] = []
        pinned_ids = request.pinned_chunk_ids or []
        if len(pinned_ids) > _MAX_PINNED_CHUNK_IDS:
            log.warning("synthesis.pinned_chunk_ids_capped",
                        request_id=str(request.request_id),
                        requested=len(pinned_ids),
                        capped_at=_MAX_PINNED_CHUNK_IDS)
            pinned_ids = pinned_ids[:_MAX_PINNED_CHUNK_IDS]
        if pinned_ids:
            pinned_chunks = await self._retrieval.recall_chunks_by_ids(
                pinned_ids, tenant_id=tenant_id,
            )
            if len(pinned_chunks) < len(pinned_ids):
                log.warning("synthesis.pinned_chunks_dropped",
                            request_id=str(request.request_id),
                            requested=len(pinned_ids),
                            resolved=len(pinned_chunks))

        retrieved_chunks: list[ResultItem] = []
        retrieval_intent_used: str | None = "pinned_only"
        retrieval_total_items: int = len(pinned_chunks)
        entity_dossier: str | None = None      # Phase 4.9 — populated by retrieval

        if not request.pinned_only:
            # Run normal retrieval to find NEW chunks for the question.
            intent_hint = _coerce_intent(request.intent_hint) or Intent.AUTO
            ret_request = RetrievalRequest(
                query_text=request.query_text,
                intent_hint=intent_hint,
                limit=max(request.max_sources, 4) * 2,
                rerank=request.rerank,
            )
            ret_result = await self._retrieval.recall(
                ret_request, tenant_id=tenant_id,
            )

            chunks = _filter_to_chunks(ret_result.items)
            # Same fallback-to-semantic guard as before — applies only
            # to the retrieved (non-pinned) portion.
            if not chunks and ret_result.intent_used != Intent.SEMANTIC:
                log.info("synthesis.fallback_to_semantic",
                         request_id=str(request.request_id),
                         original_intent=str(ret_result.intent_used),
                         original_items=len(ret_result.items))
                ret_result = await self._retrieval.recall(
                    RetrievalRequest(
                        query_text=request.query_text,
                        intent_hint=Intent.SEMANTIC,
                        limit=request.max_sources * 2,
                        rerank=request.rerank,
                    ),
                    tenant_id=tenant_id,
                )
                chunks = _filter_to_chunks(ret_result.items)

            retrieved_chunks = chunks
            retrieval_intent_used = (
                str(ret_result.intent_used) if ret_result.intent_used else None
            )
            retrieval_total_items = len(pinned_chunks) + len(ret_result.items)
            entity_dossier = ret_result.entity_dossier

        # De-duplicate: if a pinned chunk also surfaced in retrieval, the
        # PINNED entry wins (stable id position). Track seen chunk_ids.
        seen: set[UUID] = {c.id for c in pinned_chunks}
        retrieved_unique = [c for c in retrieved_chunks if c.id not in seen]

        ordered_chunks = pinned_chunks + retrieved_unique
        retrieval_ms = round((time.monotonic() - t0) * 1000, 1)

        # ─── 2. Build CitationSource[] — pinned ids 1..K then retrieved ────
        sources = _to_citation_sources(
            ordered_chunks, max_sources=request.max_sources,
        )
        citation_map = {s.id: s for s in sources}

        log.info("synthesis.retrieval_complete",
                 request_id=str(request.request_id),
                 pinned_resolved=len(pinned_chunks),
                 retrieved=len(retrieved_unique),
                 chunks_used=len(sources),
                 retrieval_ms=retrieval_ms,
                 intent_used=retrieval_intent_used,
                 pinned_only=request.pinned_only)

        yield SourcesEvent(
            sources=sources,
            chunks_total=retrieval_total_items,
            chunks_used=len(sources),
            retrieval_intent=retrieval_intent_used,
        )

        # ─── 3. Build messages + cloud-policy gate ─────────────────────────
        backend = self._llm_factory(request.model_name)
        messages = build_messages(
            user_query=request.query_text,
            sources=sources,
            persona=request.persona,
            entity_dossier=entity_dossier,
        )

        log.info("synthesis.llm_request",
                 request_id=str(request.request_id),
                 model=backend.model_version,
                 backend_kind=backend.backend_kind,
                 is_cloud=backend.is_cloud,
                 max_tokens=request.max_tokens,
                 sources=len(sources))

        # Pre-flight: refuse the call if the chunk-bag exceeds policy for
        # this cloud backend. Local backends pass through silently. The
        # gate WRITES an audit row on deny before raising, so denials are
        # forensically visible alongside successful egresses.
        await assert_cloud_allowed_for_synthesis(
            self._pool,
            tenant_id=tenant_id,
            sources=sources,
            backend=backend,
            request_allow_personal_data=request.allow_cloud_for_personal_data,
            request_allow_sensitive_data=request.allow_cloud_for_sensitive_data,
        )

        # Stream with truncation-aware continuation (Phase 4.5a).
        # If finish_reason indicates truncation, the engine re-calls the
        # LLM with the partial answer as context and "continue" prompt.
        # Up to MAX_CONTINUATIONS iterations. Each call gets its own
        # cloud-egress audit row. Token deltas keep flowing as DeltaEvents
        # — the user sees uninterrupted prose.
        raw_pieces: list[str] = []
        cumulative_usage = TokenUsage()
        finish_reason = "stop"
        continuation_count = 0
        truncated_after_continuations = False

        current_messages = list(messages)

        for iteration in range(_MAX_CONTINUATIONS + 1):
            try:
                async for delta in backend.generate_stream(
                    current_messages,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    response_format="text",
                    timeout_s=request.timeout_s,
                ):
                    if delta.done:
                        finish_reason = delta.finish_reason or "stop"
                        if delta.usage is not None:
                            cumulative_usage = _add_usage(
                                cumulative_usage, delta.usage,
                            )
                        break
                    if delta.text:
                        raw_pieces.append(delta.text)
                        yield DeltaEvent(text=delta.text)
            except Exception as call_exc:
                # Audit on error — but if the audit itself fails, swallow
                # so we don't mask the original exception. The original
                # call_exc is what the caller needs to see.
                await _audit_safely(
                    self._pool, tenant_id=tenant_id, sources=sources,
                    backend=backend, usage=None, outcome="error",
                    error_reason=type(call_exc).__name__,
                )
                raise

            # Audit each LLM call separately so operators see continuation
            # passes alongside the initial call. Audit failure is NON-fatal
            # — the cloud call already completed; failing the response
            # because the audit row didn't write would discard the user's
            # answer for an operator-side concern. Logged loud + continue.
            await _audit_safely(
                self._pool, tenant_id=tenant_id, sources=sources,
                backend=backend, usage=cumulative_usage, outcome="success",
            )

            if not _is_truncated(finish_reason):
                break  # Clean stop — done.

            # Truncation detected. If we've hit the iteration cap, record
            # and surface the partial best-effort answer.
            if iteration >= _MAX_CONTINUATIONS:
                truncated_after_continuations = True
                log.warning("synthesis.continuation_exhausted",
                            request_id=str(request.request_id),
                            iterations=iteration,
                            chars=sum(len(p) for p in raw_pieces))
                break

            # Build continuation messages: original system + user + the
            # partial answer (with any incomplete trailing tag stripped)
            # as an assistant turn + a fresh user turn requesting
            # continuation.
            continuation_count = iteration + 1
            partial = "".join(raw_pieces)
            partial_clean = _strip_incomplete_tag(partial)
            current_messages = [*list(messages), Message(role="assistant", content=partial_clean), Message(role="user", content=_CONTINUATION_PROMPT)]
            log.info("synthesis.continuation_starting",
                     request_id=str(request.request_id),
                     iteration=continuation_count,
                     prior_chars=len(partial),
                     finish_reason=finish_reason)
            yield ContinuationEvent(
                iteration=continuation_count,
                partial_chars=len(partial),
                reason=finish_reason,
            )

        # ─── 4. DoneEvent — stream finished (across all continuations) ─────
        yield DoneEvent(
            finish_reason=finish_reason,
            usage=cumulative_usage,
            continuation_count=continuation_count,
        )

        # ─── 5. Parse → AnswerEvent ────────────────────────────────────────
        raw_answer = "".join(raw_pieces)
        answer = parse_answer(
            raw_answer,
            citation_map=citation_map,
            model_version=backend.model_version,
            finish_reason=finish_reason,
            include_chunk_text=request.include_chunk_text,
        )
        answer.continuation_count = continuation_count
        answer.truncated_after_continuations = truncated_after_continuations

        log.info("synthesis.parsed",
                 request_id=str(request.request_id),
                 claims=len(answer.claims),
                 citations=len(answer.citations),
                 has_uncited=answer.has_uncited_claims,
                 has_failed=answer.has_failed_citations,
                 fallback_used=answer.fallback_pattern_used)

        yield AnswerEvent(answer=answer)

        # ─── 6. Citation verification (Phase 4.4) ───────────────────────────
        # Runs AFTER AnswerEvent so the user-visible answer renders
        # immediately while verification embeddings + cosines compute in
        # the background. UI overlays the verification status on the
        # already-rendered citations once VerifyEvent arrives.
        if self._verifier is not None and answer.citations:
            try:
                verified = await self._verifier.verify(answer, citation_map)
                citation_updates = {
                    cid: {
                        "status": cit.status,
                        "cosine": cit.cosine,
                        "reason": cit.reason,
                    }
                    for cid, cit in verified.citations.items()
                }
                yield VerifyEvent(citation_updates=citation_updates)
            except Exception as e:
                # Verification failure is non-fatal — the user already has
                # the answer. Log + continue without VerifyEvent so the UI
                # falls back to "unverified" rendering.
                log.warning("synthesis.verify_failed",
                            request_id=str(request.request_id),
                            error_class=type(e).__name__,
                            error=str(e)[:200])


# ─── Helpers ────────────────────────────────────────────────────────────────

def _coerce_intent(value: str | None) -> Intent | None:
    """Convert the request's string intent_hint to the Intent enum.
    Unknown values fall through to AUTO (which the rule classifier
    handles)."""
    if not value:
        return None
    try:
        return Intent(value.lower())
    except ValueError:
        log.warning("synthesis.unknown_intent_hint", intent_hint=value)
        return None


def _filter_to_chunks(items: list[ResultItem]) -> list[ResultItem]:
    """Keep only chunk-shaped items. Synthesis cites chunks; non-chunks
    can't be cited because they don't carry source text we can grounding-
    check against."""
    return [it for it in items if it.kind == "chunk" and it.snippet]


def _to_citation_sources(
    chunks: list[ResultItem], *, max_sources: int,
) -> list[CitationSource]:
    """Slice top-N chunks and convert to CitationSource[].

    Each CitationSource gets a 1-based id matching its position in the
    ordered chunk list. Pinned chunks come first (preserving stable
    numeric ids across follow-up turns), then retrieved chunks fill the
    remaining slots.

    `text` is the FULL chunk text — pinned chunks supply this via
    metadata.full_text (loaded from the chunks table); retrieved chunks
    only have the 280-char snippet from the vector backend, so we use
    snippet as a best-effort. If full text is needed for a retrieved
    chunk's verification, retrieval should be extended to fetch the full
    text (v0.2 follow-up; for now snippet is sufficient for both LLM
    grounding and verifier cosine).
    """
    out: list[CitationSource] = []
    for i, chunk in enumerate(chunks[:max_sources], start=1):
        meta = chunk.metadata or {}
        doc_id = meta.get("doc_id")
        if isinstance(doc_id, str):
            from uuid import UUID as _UUID
            try:
                doc_id = _UUID(doc_id)
            except ValueError:
                doc_id = None
        # Pinned chunks fetched via chunks_by_ids carry the full text in
        # metadata.full_text. Retrieved chunks have a 280-char snippet
        # only. Prefer full_text when available.
        text = meta.get("full_text") or chunk.snippet or ""
        topics_raw = meta.get("topics") or []
        topics = tuple(t for t in topics_raw if isinstance(t, str))[:3]
        out.append(CitationSource(
            id=i,
            chunk_id=chunk.id,
            doc_id=doc_id or chunk.id,
            text=text,
            title=chunk.title,
            doc_topics=topics,
            sensitivity_class=chunk.sensitivity_class or "standard",
            processing_profile=str(meta.get("processing_profile") or "custom"),
        ))
    return out


# Hard cap on pinned_chunk_ids per request (defense-in-depth — Pydantic
# at the API boundary will also enforce this). Prevents an agent from
# requesting `chunks_by_ids` lookups for thousands of UUIDs in one call.
# 50 is well above any realistic conversation pin set; agents needing more
# should be issuing a fresh retrieval call instead.
_MAX_PINNED_CHUNK_IDS = 50


# ─── Truncation continuation (Phase 4.5a) ──────────────────────────────────

# Cap on continuation iterations. Each iteration is another full LLM call.
# 3 iterations × default max_tokens (1024) = up to 4× the original budget,
# which covers the vast majority of long-answer cases. Beyond this, the
# request is probably mis-shaped (max_tokens too low for the question);
# user sees the best-effort partial + a `truncated_after_continuations`
# flag and can re-issue with a higher max_tokens.
_MAX_CONTINUATIONS = 3

# finish_reason values across providers that mean "ran out of tokens".
_TRUNCATION_REASONS = frozenset({
    "length",        # OpenAI, DeepSeek, Kimi
    "max_tokens",    # Anthropic
    "truncated",     # some compatibility layers
})

_CONTINUATION_PROMPT = (
    "Continue your answer from EXACTLY where you stopped. Do not repeat any "
    "words you already wrote. Maintain the same voice, style, and citation "
    "format (`<c id=\"N\"/>` tags after each claim). Do not restart or "
    "summarize — just keep going."
)

# Detect an incomplete trailing citation tag so we can strip it before
# feeding the partial answer back as assistant context. Without this,
# the next call would see `... <c id="3` and might emit nonsense to
# "complete" it. Examples this matches:
#   `<c`, `<c id`, `<c id=`, `<c id="`, `<c id="3"`, `<c id="3" /`
_INCOMPLETE_TAG_RE = re.compile(
    r"<c(?:\s|\s+id\s*=?\s*[\"']?\d*[\"']?\s*/?)?$",
    re.IGNORECASE,
)


def _is_truncated(finish_reason: str | None) -> bool:
    """Return True if the LLM stopped because it ran out of tokens
    (vs. naturally completing the answer)."""
    return (finish_reason or "").lower() in _TRUNCATION_REASONS


def _strip_incomplete_tag(partial: str) -> str:
    """Remove a trailing partial citation tag if present.

    The LLM might have been mid-tag when it ran out of tokens. Feeding
    the partial back with a broken tag confuses the continuation, so we
    cut anything that looks like an open `<c id="...` without a closing
    `/>` from the end of the string. Whitespace immediately preceding
    the partial tag is preserved so the continuation flows naturally.
    """
    return _INCOMPLETE_TAG_RE.sub("", partial)


def _add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    """Sum two TokenUsage instances. Used to accumulate across continuation
    LLM calls so DoneEvent.usage reflects the total cost."""
    return TokenUsage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        total_tokens=a.total_tokens + b.total_tokens,
    )


async def _audit_safely(
    pool: PostgresPool,
    *,
    tenant_id: UUID,
    sources: list[CitationSource],
    backend: LlmBackend,
    usage: TokenUsage | None,
    outcome: str, error_reason: str | None = None,
) -> None:
    """Wrap audit_synthesis_cloud_egress so a DB-side audit failure can't
    fail the synthesis response. The cloud call already happened; the
    user already saw (or is about to see) the answer. Refusing to deliver
    because the audit row didn't write is the wrong tradeoff.

    Loud-log on failure so operators can investigate. Future v0.2 work:
    queue failed audit rows for retry rather than dropping them.
    """
    try:
        await audit_synthesis_cloud_egress(
            pool, tenant_id=tenant_id, sources=sources,
            backend=backend, usage=usage,
            outcome=outcome, error_reason=error_reason,
        )
    except Exception as audit_exc:
        log.error("synthesis.audit_write_failed",
                  outcome=outcome, error_reason=error_reason,
                  audit_error=type(audit_exc).__name__,
                  audit_msg=str(audit_exc)[:200])


def _is_retriable(e: BaseException) -> bool:
    """Best-effort retriable classifier. Network errors and timeouts are
    retriable; programming errors and validation errors are not.

    CloudPolicyDenied is explicitly non-retriable — same call body will
    fail the same way until tenant policy or request flags change.
    """
    if isinstance(e, CloudPolicyDenied):
        return False
    name = type(e).__name__
    if name in {"TimeoutError", "ConnectError", "ReadTimeout", "WriteTimeout",
                "ConnectTimeout", "PoolTimeout", "RemoteProtocolError"}:
        return True
    # httpx 5xx
    if name == "HTTPStatusError":
        try:
            return bool(500 <= e.response.status_code < 600)  # type: ignore[attr-defined]
        except AttributeError:
            return False
    return False
