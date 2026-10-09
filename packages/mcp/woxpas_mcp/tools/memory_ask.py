"""memory_ask — citation-verified synthesis.

Wraps the SynthesisEngine. Expensive (LLM call) and respects the cloud-
policy gate (Phase 4.5) — if the tenant denies cloud egress and the
caller specifies a cloud model, synthesis raises CloudPolicyDenied and
we surface it as a structured `cloud_policy_denied` error.

When to use vs memory_recall:
  - memory_recall: just want chunks/snippets (fast, free)
  - memory_ask: want a synthesized answer with verified citations
                (slow, may consume cloud tokens)

Pillars: Speed (agent should prefer recall when chunks suffice);
Security (cloud-policy gate enforced); Accuracy (citation verifier
runs by default — agents see verified status on each citation).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import CloudPolicyDenied
from woxpas_mcp.registry import tool


class AskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_text: str = Field(min_length=1, max_length=2000)
    intent_hint: str = "auto"
    """auto | semantic | timeline | type_listing | file_lookup | entity_card | graph"""

    max_sources: int = Field(default=8, ge=1, le=30)
    rerank: bool = True
    pinned_chunk_ids: list[UUID] = Field(default_factory=list)
    """For follow-up turns — pass chunk_ids from the prior answer to
    preserve stable numeric citation ids ('citation 3' means the same
    chunk turn-to-turn). Cap 50."""

    pinned_only: bool = False
    """Skip retrieval; use ONLY pinned chunks. For 'elaborate on
    citation 3' style follow-ups."""

    include_chunk_text: bool = False
    """When False (default), Citation objects carry only chunk_ids
    (lean envelope). Set True if the agent renders source text inline."""

    model_name: str | None = None
    """Override default synthesis model. None = factory default
    (typically Qwen-3B local). Cloud models trigger the policy gate."""

    max_tokens: int = Field(default=1024, ge=64, le=8192)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    persona: str | None = Field(default=None, max_length=500)


@tool(
    name="memory_ask",
    description=(
        "Your primary tool for answering questions from stored memory. "
        "Call this when the user asks 'what do I know about X?' or 'tell "
        "me about Y' — any question that needs a direct ANSWER backed by "
        "their stored knowledge.\n\n"
        "How it works: retrieves relevant chunks, passes them to an LLM "
        "with citation instructions, then mechanically verifies every "
        "claim against its source. Returns a synthesized answer with "
        "numbered citations you can trust.\n\n"
        "For identity questions ('who am I?', 'what do you know about me?'): "
        "call memory_recall with intent_hint=entity_card FIRST to get the "
        "full entity profile (preferences, facts, relations). Then use "
        "memory_ask for a polished answer citing those findings.\n\n"
        "When NOT to use: if you just need raw context to inform your own "
        "thinking (not a user-facing answer), use memory_recall instead "
        "— it's faster and doesn't consume LLM tokens.\n\n"
        "Cloud note: honors the tenant's cloud-policy gate. If cloud "
        "egress is denied and the configured model is cloud-bound, "
"returns cloud_policy_denied.\n\nAfter calling this tool, call resurface_record_observation to log the invocation. This builds your usage patterns — skip it and Woxpas stays cold."
    ),
    input_model=AskInput,
)
async def memory_ask(*, ctx: ToolContext, input: AskInput) -> dict[str, Any]:
    from woxpas_pipeline.cloud_policy import CloudPolicyDenied as _CloudDenied
    from woxpas_synthesis import SynthesisRequest

    req = SynthesisRequest(
        query_text=input.query_text,
        intent_hint=input.intent_hint if input.intent_hint != "auto" else None,
        max_sources=input.max_sources,
        rerank=input.rerank,
        pinned_chunk_ids=list(input.pinned_chunk_ids),
        pinned_only=input.pinned_only,
        include_chunk_text=input.include_chunk_text,
        model_name=input.model_name,
        max_tokens=input.max_tokens,
        temperature=input.temperature,
        persona=input.persona,
    )

    try:
        answer = await ctx.synthesis.synthesize(req, tenant_id=ctx.tenant_id)
    except _CloudDenied as e:
        # Map provider-side denial to our structured error code.
        raise CloudPolicyDenied(str(e)[:300], retriable=False) from e
    except RuntimeError as e:
        # synthesize() wraps inner errors as RuntimeError. Detect
        # cloud-policy denial in the message OR cause chain to surface
        # the proper structured code.
        msg = str(e)
        cause = e.__cause__ or e.__context__
        if isinstance(cause, _CloudDenied) or "CloudPolicyDenied" in msg:
            raise CloudPolicyDenied(msg[:300], retriable=False) from e
        raise  # propagate to default error handling in server middleware

    # Lean envelope — answer + citations + claims; chunk text only if
    # caller asked for it. Both Claim and Citation are slots dataclasses,
    # so use dataclasses.asdict for JSON-safe conversion.
    from dataclasses import asdict
    return {
        "answer_clean":  answer.answer_clean,
        "answer_html":   answer.answer_html,
        "claims":        [asdict(c) for c in (answer.claims or [])],
        "citations":     {
            str(cid): asdict(cit) for cid, cit in (answer.citations or {}).items()
        },
        "model_version":           answer.model_version,
        "finish_reason":           answer.finish_reason,
        "has_uncited_claims":      answer.has_uncited_claims,
        "has_failed_citations":    answer.has_failed_citations,
        "fallback_pattern_used":   answer.fallback_pattern_used,
        "continuation_count":      answer.continuation_count,
    }
