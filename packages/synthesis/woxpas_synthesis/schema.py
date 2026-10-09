"""Synthesis output schema — citation-bound answer + verifiable claims.

The synthesis pipeline emits a structured answer that is:
  1. STREAMABLE — the LLM produces prose that streams to the user as it
     arrives. Citation markers are emitted INLINE as `<c id="N"/>` self-closing
     XML tags. The user sees real-time prose rendering; no JSON-parsing
     blocks the visual stream.
  2. VERIFIABLE — every numeric citation id maps to a real chunk_id from
     the retrieval result. Post-stream verification (Phase 4.4) runs
     substring + cosine checks per citation and stamps each with
     verified=true|false. Failed citations become a UI signal, not a
     content rewrite.
  3. PROVENANCE-PRESERVING — citations carry chunk_id + doc_id + (optionally)
     a span offset, so the UI can deep-link to the source span on click.

Why integer ids and not raw chunk UUIDs:
  LLMs are well-known for mangling 32-character UUIDs (truncation, character
  swaps, hallucinated digits). Sequential integers (1, 2, ...) survive the
  generation process intact. We pre-build a citation_map { 1: chunk_id_a, ... }
  that the prompt presents to the model as `<source id="1">...</source>`,
  and the model reciprocates with `<c id="1"/>` in its output.

Why `<c id="N"/>` and not `[1]`:
  XML-style tags resist tokenizer drift better than bracketed integers
  (which split into `[`, `1`, `]` and can drift into `[ 1 ]` or `1.`).
  Anthropic's prompt guidance explicitly recommends XML tags; Claude family
  is heavily trained on them. OpenAI/DeepSeek/Qwen also handle them well.
  Self-closing form `<c id="N"/>` is unambiguous to a single-pass regex
  parser and won't collide with markdown formatting in the answer prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

# ─── The shape of one cited source presented to the LLM ─────────────────────

@dataclass(slots=True, frozen=True)
class CitationSource:
    """One source the LLM is allowed to cite.

    Built once per synthesis call from the retrieval result. The integer
    `id` is what the model echoes back as `<c id="N"/>` — we keep the real
    chunk_id + doc_id internal so verification can resolve back to the
    on-disk chunk text.

    `text` holds the FULL chunk text — the LLM sees it, the verifier
    grounds against it. This is internal to a single synthesis call;
    it does NOT ship in the response by default (token cost — see
    SynthesisRequest.include_chunk_text for the heavy-envelope opt-in).
    """
    id:          int                      # 1-based, sequential
    chunk_id:    UUID
    doc_id:      UUID
    text:        str                      # full chunk text (LLM input + verifier ground truth)
    title:       str | None = None        # doc title (for prompt + agent attribution)
    doc_topics:  tuple[str, ...] = ()     # frozen for hashability; first 1-3 doc topics
    span:        tuple[int, int] | None = None    # offset within the chunk (rare; v0.2)
    # Sensitivity classification — used by the cloud-policy gate to decide
    # whether this chunk can be sent to a cloud LLM. Bag-of-chunks gating
    # uses the highest tier across all sources (worst-case).
    sensitivity_class:  str = "standard"     # standard | personal_data | sensitive
    processing_profile: str = "custom"       # personal | identity_seed | correspondence | code | etc.


# ─── Output: parsed claims + verified citations ─────────────────────────────

VerificationStatus = Literal[
    "verified",            # substring match AND cosine ≥ threshold
    "weak",                # cosine ≥ threshold but no substring match
    "unverified",          # cosine < threshold (or no chunk text overlap at all)
    "fabricated",          # citation id not in citation_map at all
    "uncited",             # claim has no citation tag
]


@dataclass(slots=True)
class Citation:
    """A single citation as it appears in the answer — LEAN envelope.

    Carries identity (chunk_id + doc_id) and attribution (doc_title +
    doc_topics + chunk_label) — enough for the agent to confidently
    attribute ("this came from phoenix-roadmap.md, in the section about
    migration timing") and for follow-up turns to pin the same chunks
    via SynthesisRequest.pinned_chunk_ids.

    Full chunk text is NOT included by default — token cost on the agent's
    context window. Caller opts in via SynthesisRequest.include_chunk_text=True
    when they need it (e.g., a UI rendering all source text inline). For
    on-demand quote fetch, agents call GET /chunks/<chunk_id> (Phase 4.6).

    citation_id is the integer the LLM emitted (`<c id="N"/>`). It is
    request-local: the same chunk_id can have a different citation_id in
    a different turn. Persistent references should use chunk_id, not
    citation_id."""
    citation_id: int                                  # what the LLM emitted (request-local)
    chunk_id:    UUID | None  = None                  # None when fabricated
    doc_id:      UUID | None  = None                  # None when fabricated
    status:      VerificationStatus = "unverified"
    reason:      str | None = None                    # why it failed (if it did)
    cosine:      float | None = None                  # populated by verifier

    # Attribution fields — cheap on the wire, sufficient for agents to
    # describe what was retrieved without re-fetching.
    doc_title:   str | None = None
    doc_topics:  list[str]  = field(default_factory=list)   # first 1-3 doc topics
    chunk_label: str | None = None                          # ~80 char "what is this chunk about"

    # Heavy-envelope opt-in. None unless include_chunk_text=True on the
    # request. Used by UI/research surfaces that render source text inline.
    chunk_text:  str | None = None


@dataclass(slots=True)
class Claim:
    """One sentence of the answer + its citation set.

    The synthesis parser splits the answer into sentence-shaped claims and
    associates each with the citation tags it contained. The verifier then
    looks at each claim's cited chunks and decides whether the sentence is
    actually grounded in the cited source text.
    """
    text:         str                                 # the claim sentence (tags stripped)
    citation_ids: list[int]                           # citation ids appearing in the claim
    status:       VerificationStatus = "unverified"   # roll-up across cited chunks
    reason:       str | None = None


@dataclass(slots=True)
class SynthesizedAnswer:
    """Final synthesis output, with multiple rendered forms for different
    surfaces.

    Renderings:
      answer_raw       — exact LLM stream output, tags intact (audit)
      answer_clean     — text only, all tags stripped (for copy/paste)
      answer_html      — `<sup><a href="#cite-N">[N]</a></sup>` markers
                         + status-aware CSS classes (browsers)
      answer_terminal  — `[N]` bracketed markers (no styling — call
                         render_terminal_with_ansi() for colored output)

    `claims` and `citations` are the verifiable structure. After Phase 4.4
    runs, every Citation has a status + cosine; every Claim has a roll-up
    status. The UI can color-code or footnote based on those.

    `model_version`, `synthesis_profile_version` stamp the output for
    re-run targeting (same pattern as summarize_profile_version).
    """
    answer_raw:                str
    answer_clean:              str
    answer_html:               str
    answer_terminal:           str
    claims:                    list[Claim]
    citations:                 dict[int, Citation]    # citation_id → Citation
    model_version:             str
    synthesis_profile_version: str
    finish_reason:             str
    # Aggregate signals for UI / observability
    has_uncited_claims:        bool = False
    has_failed_citations:      bool = False
    fallback_pattern_used:     bool = False           # parser fell back to non-XML form
    fallback_pattern_count:    int  = 0
    # Truncation-recovery signals (Phase 4.5a)
    continuation_count:        int  = 0   # how many continuation LLM calls were needed
    truncated_after_continuations: bool = False  # still cut short after max iterations


# Bumped on prompt or schema change → triggers targeted re-synthesis on docs
# that ran under an older synthesis prompt.
#   0.1.0 — initial citation-bound prose synthesis with <c id="N"/> tags
SYNTHESIS_PROFILE_VERSION = "0.1.0"
