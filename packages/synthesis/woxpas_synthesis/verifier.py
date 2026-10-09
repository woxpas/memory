"""Citation verifier — grounds each cited claim against its cited chunk.

Two checks per citation, run in parallel-batchable form:

  1. LEXICAL OVERLAP — do meaningful word n-grams from the claim appear in
     the chunk text? Robust against paraphrasing tolerance: catches direct
     quotes AND near-quotes. Fast, no model calls. Single signal that
     confirms "the model didn't make this up".

  2. COSINE SIMILARITY — embed the claim sentence, compute cosine to the
     chunk text embedding. Catches semantic equivalence the lexical check
     misses ("the team finished" vs "completion happened"). Slower but
     necessary for the cases where the LLM correctly summarized a chunk
     using different words.

Verdict per citation:
   substring_match  AND  cosine ≥ threshold  →  "verified"
                          cosine ≥ threshold  →  "weak"        (semantic only)
                          cosine <  threshold  →  "unverified"  (no grounding)

Already-flagged "fabricated" citations (id not in retrieval result) skip
verification — there's no chunk to compare against. The parser sets that
status at parse time.

Per-claim roll-up:
  any verified citation                     →  "verified"
  any weak (and no fail)                    →  "weak"
  any cosine-fail OR all fabricated         →  "unverified"
  no citations at all                       →  "uncited"

Why this choice over alternatives:
  - LLM-as-judge for verification was considered. Rejected: same model
    that fabricated a claim might also vouch for it. Mechanical signals
    (substring + cosine) are deterministic and don't depend on the
    LLM's good behavior.
  - Token-level overlap was considered. Rejected: too brittle ("Sarah"
    and "Sarah's" don't match without stemming). Word n-grams are
    forgiving enough for paraphrasing while catching direct copying.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import structlog
from woxpas_runtime import EmbedderBackend

from woxpas_synthesis.schema import (
    CitationSource,
    SynthesizedAnswer,
    VerificationStatus,
)

log = structlog.get_logger(__name__)


# ─── Stopword list — short common words filtered before n-gram match ───────
# Not exhaustive — enough to bias n-grams toward content words. We only
# use this for the "do any 4-grams overlap" check; it's tolerant by design.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "by", "for", "with", "as", "from",
    "and", "or", "but", "not", "no", "do", "does", "did", "have", "has",
    "had", "this", "that", "these", "those", "it", "its", "i", "you",
    "he", "she", "we", "they", "them", "him", "her", "his", "their",
    "my", "our", "your", "me", "us", "so", "if", "then", "than", "into",
    "out", "up", "down", "over", "under", "about", "what", "who", "when",
    "where", "why", "how",
})


# ─── Tunables ──────────────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class VerifierConfig:
    """Verifier thresholds. Defaults are tuned for English prose grounded
    in chunk-text up to ~1500 chars."""
    cosine_threshold:        float = 0.55       # claim ↔ chunk cosine
    ngram_size:              int   = 4          # word n-gram window
    ngram_short_fallback:    int   = 3          # for claims shorter than ngram_size
    ngram_min_match:         int   = 1          # ≥1 matching n-gram → substring_match


# ─── Public API ────────────────────────────────────────────────────────────

class CitationVerifier:
    """Runs lexical + cosine verification over a SynthesizedAnswer.

    Reusable across requests. Holds an EmbedderBackend handle. Single
    verify() call batches ALL claim and chunk embeddings together for one
    embedder pass — much faster than per-claim calls.
    """

    def __init__(
        self,
        embedder: EmbedderBackend,
        *,
        config: VerifierConfig | None = None,
    ) -> None:
        self._embedder = embedder
        self._config   = config or VerifierConfig()

    async def verify(
        self,
        answer: SynthesizedAnswer,
        citation_map: dict[int, CitationSource],
    ) -> SynthesizedAnswer:
        """Mutate answer.citations and answer.claims with verification
        results. Returns the same answer for chaining.

        Process:
          1. For each claim: collect (claim_text, [cited_chunk_texts])
          2. Run a SINGLE embedder batch over all unique strings
          3. Compute pairwise cosines
          4. Run lexical n-gram overlap (no embedding needed)
          5. Stamp each Citation's status + cosine
          6. Roll-up each Claim's status from its citations
        """
        if not answer.citations:
            log.debug("verifier.skip_no_citations")
            return answer

        t0 = time.monotonic()

        # Build the encoding batch: every unique string we need to embed.
        # We index by string so duplicates collapse.
        text_to_index: dict[str, int] = {}
        all_texts: list[str] = []

        def _enroll(s: str) -> int:
            if s not in text_to_index:
                text_to_index[s] = len(all_texts)
                all_texts.append(s)
            return text_to_index[s]

        # Collect all claim texts
        claim_indices: list[int] = []
        for claim in answer.claims:
            claim_indices.append(_enroll(claim.text))

        # Collect chunk texts for each citation that has a chunk.
        # Skip already-fabricated citations — no chunk to verify against.
        citation_chunk_idx: dict[int, int] = {}
        for cid, cit in answer.citations.items():
            if cit.status == "fabricated":
                continue
            src = citation_map.get(cid)
            if src is None:
                # Map missing — flag fabricated; this should be rare since
                # parse already does this lookup, but defensive.
                cit.status = "fabricated"
                cit.reason = f"id {cid} not in citation_map at verification time"
                continue
            citation_chunk_idx[cid] = _enroll(src.text)

        # If no real citations to verify (everything fabricated), we're done.
        if not citation_chunk_idx:
            log.info("verifier.all_fabricated", citations=len(answer.citations))
            self._roll_up_claims(answer)
            return answer

        # Single batch embed call.
        embeddings = await self._embed_batch(all_texts)
        ms_embed = round((time.monotonic() - t0) * 1000, 1)

        # ─── 2. Score each citation independently ──────────────────────────
        for cid, cit in answer.citations.items():
            if cit.status == "fabricated":
                continue
            src = citation_map.get(cid)
            if src is None:
                continue

            chunk_idx = citation_chunk_idx[cid]
            chunk_vec = embeddings[chunk_idx]

            # A citation can ground multiple claims (same id used in
            # different sentences). For each occurrence, compute the
            # cosine + lexical match against the claim it appears in,
            # and roll up the BEST result to the citation's status.
            best_cosine: float = 0.0
            best_lexical: bool = False
            for claim, claim_idx in zip(answer.claims, claim_indices, strict=True):
                if cid not in claim.citation_ids:
                    continue
                claim_vec = embeddings[claim_idx]
                cos = _cosine(claim_vec, chunk_vec)
                lex = _lexical_overlap(
                    claim.text, src.text,
                    n=self._config.ngram_size,
                    n_short=self._config.ngram_short_fallback,
                )
                if cos > best_cosine:
                    best_cosine = cos
                if lex:
                    best_lexical = True

            cit.cosine = round(best_cosine, 4)
            cit.status = self._classify(
                cosine=best_cosine, lexical=best_lexical,
            )
            cit.reason = self._build_reason(
                cosine=best_cosine, lexical=best_lexical, status=cit.status,
            )

        # ─── 3. Roll up claim-level statuses ───────────────────────────────
        self._roll_up_claims(answer)

        ms_total = round((time.monotonic() - t0) * 1000, 1)
        log.info("verifier.done",
                 claims=len(answer.claims),
                 citations=len(answer.citations),
                 unique_texts=len(all_texts),
                 ms_embed=ms_embed, ms_total=ms_total,
                 cosine_threshold=self._config.cosine_threshold)
        return answer

    # ─── private ────────────────────────────────────────────────────────────

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts and return a list of dense vectors.

        EmbedderBackend.embed returns an EmbeddingBatch whose `dense`
        rows are already L2-normalized by the BGE-M3 backend (cosine
        math reduces to dot product). We unwrap each row to a plain
        Python list of floats so cosine() doesn't need numpy."""
        batch = await self._embedder.embed(texts)
        out: list[list[float]] = []
        for row in batch.dense:
            out.append(list(row))
        return out

    def _classify(self, *, cosine: float, lexical: bool) -> VerificationStatus:
        if cosine < self._config.cosine_threshold:
            return "unverified"
        if lexical:
            return "verified"
        return "weak"

    def _build_reason(
        self, *, cosine: float, lexical: bool, status: VerificationStatus,
    ) -> str | None:
        if status == "verified":
            return None
        if status == "weak":
            return (
                f"semantic match (cosine={cosine:.2f}) but no n-gram overlap — "
                "claim phrasing differs from source"
            )
        if status == "unverified":
            return f"cosine={cosine:.2f} < threshold={self._config.cosine_threshold:.2f}"
        return None

    def _roll_up_claims(self, answer: SynthesizedAnswer) -> None:
        """Compute each Claim's overall status from its cited citations.
        Best-of policy: a single verified citation grounds the whole claim,
        even if other citations on the same claim are weaker."""
        for claim in answer.claims:
            if not claim.citation_ids:
                claim.status = "uncited"
                claim.reason = "claim has no citation tag"
                continue

            statuses = [
                answer.citations[cid].status
                for cid in claim.citation_ids
                if cid in answer.citations
            ]
            if not statuses:
                claim.status = "fabricated"
                claim.reason = "all citation ids unresolved"
                continue
            if any(s == "verified" for s in statuses):
                claim.status = "verified"
                claim.reason = None
            elif any(s == "weak" for s in statuses):
                claim.status = "weak"
                claim.reason = "best citation is semantic-only"
            elif all(s == "fabricated" for s in statuses):
                claim.status = "fabricated"
                claim.reason = "all cited ids fabricated"
            else:
                claim.status = "unverified"
                claim.reason = "no citation passed cosine threshold"

        # Update aggregate flags. has_uncited_claims is set by parse;
        # re-check has_failed_citations after verification.
        answer.has_failed_citations = any(
            c.status in {"fabricated", "unverified"}
            for c in answer.citations.values()
        )


# ─── Math + lexical helpers ─────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity for two equal-length vectors. Inputs are expected
    to already be L2-normalized (the embedder's `normalize=True` ensures
    this), so cosine reduces to the dot product."""
    if len(a) != len(b):
        return 0.0
    s = 0.0
    for x, y in zip(a, b, strict=True):  # lengths guarded equal just above
        s += x * y
    # Clamp into [0, 1] — rounding can drift a hair past 1.0
    if s < 0.0:
        return 0.0
    if s > 1.0:
        return 1.0
    return s


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokenize(text) if t not in _STOPWORDS and len(t) >= 2]


def _lexical_overlap(
    claim: str, chunk: str, *, n: int = 4, n_short: int = 3,
) -> bool:
    """Return True if any word n-gram from the claim appears in the chunk.

    BOTH sides are filtered to content tokens (stopwords removed) before
    n-gram matching. Otherwise a claim like "Sarah led migration"
    (stopwords stripped → "sarah led migration") wouldn't match a chunk
    saying "Sarah led THE migration" because "the" is between "led" and
    "migration" in the chunk's raw token stream. Filtering both sides
    keeps the comparison apples-to-apples.

    Falls back from `n` to `n_short` for short claims, then to 1-gram
    for single-content-word claims.
    """
    claim_tokens = _content_tokens(claim)
    if not claim_tokens:
        return False

    chunk_tokens = _content_tokens(chunk)
    if not chunk_tokens:
        return False

    # Adjust n for short claims
    window = n
    if len(claim_tokens) < window:
        window = max(n_short, 1)
    if len(claim_tokens) < window:
        window = 1

    # Both sides as space-joined content-token streams
    chunk_joined = " ".join(chunk_tokens)
    for i in range(len(claim_tokens) - window + 1):
        ngram = " ".join(claim_tokens[i:i + window])
        if ngram in chunk_joined:
            return True

    return False
