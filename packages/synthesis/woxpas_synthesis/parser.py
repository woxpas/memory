"""Citation tag parser — extract claims + citations from streamed prose.

The synthesis LLM emits answer text with inline `<c id="N"/>` self-closing
tags. This module parses that text into:
  - answer_clean    text with all tags stripped
  - answer_html     text with tags rendered as <sup><a> footnotes
  - claims          [(sentence_text, [citation_ids]), ...]
  - citations       dict[int, Citation]   — deduplicated by id

Three classes of citation pattern handled:
  1. Primary form  <c id="N"/>           preferred, always parsed
  2. Fallback A    [c:N] or [c_N]        when LLM drifts toward bracketed form
  3. Fallback B    [N]                   last-resort fallback for stubborn models

Telemetry on which form was used is reported via fallback_pattern_used /
fallback_pattern_count fields in SynthesizedAnswer — high fallback usage
signals the model is drifting and the prompt needs strengthening.

Citation deduplication:
  - CONSECUTIVE identical tags (`<c id="1"/><c id="1"/>`) collapse to one.
    Pure LLM redundancy; the verifier doesn't need duplicates of the same
    (sentence, source) pair within one position.
  - DISTANT duplicates across sentences are KEPT. Each grounds its own
    claim; verification runs per-claim.
  - The `citations` dict is naturally keyed by citation_id, so the
    References panel built from it shows each source ONCE regardless of
    how many inline mentions exist.
"""

from __future__ import annotations

import html
import re
from re import Pattern

from woxpas_synthesis.schema import (
    SYNTHESIS_PROFILE_VERSION,
    Citation,
    CitationSource,
    Claim,
    SynthesizedAnswer,
)

# ─── Patterns ───────────────────────────────────────────────────────────────

# Primary: <c id="N"/> with optional whitespace, double or single quotes
_PRIMARY: Pattern[str] = re.compile(
    r'<c\s+id\s*=\s*["\'](\d+)["\']\s*/>',
    re.IGNORECASE,
)

# Fallback patterns are ALSO consulted, in priority order. We log telemetry
# when a fallback fires.
_FALLBACK_BRACKETED: Pattern[str] = re.compile(r'\[c[:_](\d+)\]', re.IGNORECASE)
_FALLBACK_NUMERIC:   Pattern[str] = re.compile(r'\[(\d{1,3})\]')   # cap at 999 ids


# Sentence boundary — basic split. Doesn't try to be perfect; we associate
# citation tags with the sentence they end. False boundaries from
# abbreviations are rejoined post-split via _is_abbreviation_fragment so
# `Dr. Smith led the team` doesn't become a phantom uncited "Dr." claim.
_SENTENCE_SPLIT: Pattern[str] = re.compile(r'(?<=[.!?])\s+(?=[A-Z<\[\(])')

# Common abbreviations that end in `.` and are routinely split as their
# own "sentence" by the regex above. When the previous fragment matches
# one of these patterns, merge it with the next.
#
#   Titles:        Dr Mr Mrs Ms Prof Sr Jr St Rev Hon
#   Initials:      A. R. J.  (single letter + period)
#   Short Latin:   e.g. i.e. etc vs cf
#   Company:       Inc Ltd Corp Co LLC
#   Geo:           U.S. U.K. (and similar, caught by single-letter rule
#                  applied to the LAST element)
_ABBREV_TITLE = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "rev", "hon",
    "fr", "sgt", "lt", "capt", "gen", "pvt", "cpl", "col",
    "inc", "ltd", "corp", "co", "llc", "plc",
    "etc", "vs", "cf", "et", "al",
    "no",   # No. for "Number"
    "fig", "vol", "ed", "eds", "p", "pp", "ch", "sec",
}


# ─── Public API ─────────────────────────────────────────────────────────────

def parse(
    raw_answer:    str,
    *,
    citation_map:        dict[int, CitationSource],
    model_version:       str,
    finish_reason:       str  = "stop",
    include_chunk_text:  bool = False,
) -> SynthesizedAnswer:
    """Convert a raw streamed answer + the citation_map into a structured
    SynthesizedAnswer.

    Verification is NOT run here — that's Phase 4.4. Each Citation lands
    with status="unverified" (or "fabricated" if the id wasn't in the map).
    The citation_map's CitationSource carries chunk_id + doc_id so the
    verifier can resolve fast without re-querying the DB.

    citation_map: keyed by 1-based citation_id; built upstream from the
    retrieval result. The synthesis prompt presented these to the LLM as
    `<source id="1">...</source>` blocks.

    include_chunk_text: when True, populate Citation.chunk_text with the
    FULL chunk text. Default False — the lean envelope ships only
    identity + attribution to keep token cost down on the agent's
    context window.
    """
    # 1. Find every citation tag in the raw text. We walk forward and
    # collapse consecutive identical-id tags as we go. Track the form used
    # for telemetry.
    tags = _find_all_tags(raw_answer)

    fallback_count = sum(1 for t in tags if t.fallback)
    fallback_used  = fallback_count > 0

    # 2. Build the citations dict — deduplicated by citation_id.
    # Fabricated ids (not in the map) get a Citation with status="fabricated"
    # so the UI can flag them; the verifier will skip them.
    citations: dict[int, Citation] = {}
    for tag in tags:
        if tag.citation_id in citations:
            continue
        src = citation_map.get(tag.citation_id)
        if src is None:
            citations[tag.citation_id] = Citation(
                citation_id=tag.citation_id,
                status="fabricated",
                reason=f"id {tag.citation_id} not in citation_map "
                       f"(valid ids: 1..{max(citation_map.keys()) if citation_map else 0})",
            )
        else:
            citations[tag.citation_id] = Citation(
                citation_id=tag.citation_id,
                chunk_id=src.chunk_id,
                doc_id=src.doc_id,
                status="unverified",
                doc_title=src.title,
                doc_topics=list(src.doc_topics)[:3],   # cap at 3 to bound payload
                chunk_label=_first_n_chars(src.text, 80),
                chunk_text=src.text if include_chunk_text else None,
            )

    # 3. Build the cleaned answer (tags stripped) and the html answer
    # (tags rendered). We strip after collapsing consecutive duplicates so
    # `<c id="1"/><c id="1"/>` doesn't leave behind a phantom space.
    answer_collapsed = _collapse_consecutive_duplicates(raw_answer, tags)

    answer_clean    = _strip_tags(answer_collapsed)
    answer_html     = _render_html(answer_collapsed, citations)
    answer_terminal = _render_terminal(answer_collapsed)

    # 4. Split the COLLAPSED answer into claims and associate each claim
    # with the citation_ids that appear inside it.
    claims = _split_into_claims(answer_collapsed)

    has_uncited      = any(not c.citation_ids for c in claims)
    has_fabricated   = any(cit.status == "fabricated" for cit in citations.values())

    return SynthesizedAnswer(
        answer_raw=raw_answer,
        answer_clean=answer_clean,
        answer_html=answer_html,
        answer_terminal=answer_terminal,
        claims=claims,
        citations=citations,
        model_version=model_version,
        synthesis_profile_version=SYNTHESIS_PROFILE_VERSION,
        finish_reason=finish_reason,
        has_uncited_claims=has_uncited,
        has_failed_citations=has_fabricated,         # other failures populated by verifier
        fallback_pattern_used=fallback_used,
        fallback_pattern_count=fallback_count,
    )


# ─── Internals ─────────────────────────────────────────────────────────────

class _Tag:
    """One citation tag found in the raw answer. start/end are character
    offsets into the original raw string."""
    __slots__ = ("citation_id", "end", "fallback", "raw_text", "start")

    def __init__(
        self, *, start: int, end: int, citation_id: int,
        raw_text: str, fallback: bool,
    ) -> None:
        self.start = start
        self.end = end
        self.citation_id = citation_id
        self.raw_text = raw_text
        self.fallback = fallback


def _find_all_tags(text: str) -> list[_Tag]:
    """Walk the text in priority order: primary → fallback A → fallback B.
    Return tags sorted by start offset.

    Fallbacks are ONLY accepted if they don't overlap with already-found
    primaries. So a primary tag wins over a fallback at the same position.
    """
    tags: list[_Tag] = []
    occupied: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in occupied)

    for m in _PRIMARY.finditer(text):
        tags.append(_Tag(
            start=m.start(), end=m.end(),
            citation_id=int(m.group(1)),
            raw_text=m.group(0), fallback=False,
        ))
        occupied.append((m.start(), m.end()))

    for m in _FALLBACK_BRACKETED.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        tags.append(_Tag(
            start=m.start(), end=m.end(),
            citation_id=int(m.group(1)),
            raw_text=m.group(0), fallback=True,
        ))
        occupied.append((m.start(), m.end()))

    for m in _FALLBACK_NUMERIC.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        # Cap to 3-digit ids — bracketed numerics that look like years
        # ("[2023]") or large numbers shouldn't be misread as citations.
        cid = int(m.group(1))
        if cid > 999 or cid < 1:
            continue
        tags.append(_Tag(
            start=m.start(), end=m.end(),
            citation_id=cid,
            raw_text=m.group(0), fallback=True,
        ))
        occupied.append((m.start(), m.end()))

    tags.sort(key=lambda t: t.start)
    return tags


def _collapse_consecutive_duplicates(text: str, tags: list[_Tag]) -> str:
    """Collapse runs of consecutive identical-id tags by keeping only the
    first.

    `<c id="1"/><c id="1"/>` → `<c id="1"/>`
    `<c id="1"/><c id="1"/><c id="1"/>` → `<c id="1"/>`   (works at any length)
    `<c id="1"/> <c id="1"/>` (whitespace between) → `<c id="1"/> `
    `<c id="1"/><c id="2"/>` → unchanged (different ids = citation group)
    Same id with intervening NON-WHITESPACE prose → unchanged (each grounds
    its own claim; verifier needs both).

    Algorithm: walk tags in order. Each tag pairs against the most recent
    KEPT tag (not the previous one in the input list, which may have been
    dropped). If the previous-kept tag has the same id and only whitespace
    between them, drop the current tag.
    """
    if not tags:
        return text

    drop_ranges: list[tuple[int, int]] = []
    dropped_spans: list[tuple[int, int]] = []   # tag spans dropped earlier in this loop
    last_kept: _Tag | None = None
    for tag in tags:
        if last_kept is not None:
            # Compute between but treat already-dropped tag spans as
            # whitespace — so 3-in-a-row collapses cleanly: tag[0] keeps,
            # tag[1] drops, tag[2] sees only "dropped + whitespace"
            # between itself and tag[0] and also drops.
            raw_between = text[last_kept.end:tag.start]
            between = _erase_spans_in_window(
                raw_between, last_kept.end, tag.start, dropped_spans,
            )
            if last_kept.citation_id == tag.citation_id and between.strip() == "":
                # Drop ONLY the tag itself — leave any whitespace intact
                # (a trailing pass collapses doubled spaces). Drop_ranges
                # must NOT overlap, since we apply them sequentially below.
                drop_ranges.append((tag.start, tag.end))
                dropped_spans.append((tag.start, tag.end))
                continue
        last_kept = tag

    if not drop_ranges:
        return text

    # Apply drops in reverse so earlier offsets remain valid.
    out = text
    for start, end in reversed(drop_ranges):
        out = out[:start] + out[end:]
    return out


def _erase_spans_in_window(
    window:        str,
    window_start:  int,
    window_end:    int,
    drop_spans:    list[tuple[int, int]],
) -> str:
    """Replace any drop-span overlap inside [window_start, window_end) with
    spaces, so .strip() correctly sees the window as whitespace when
    everything between two kept tags is either whitespace or already-dropped
    tags."""
    if not drop_spans:
        return window
    chars = list(window)
    for ds, de in drop_spans:
        # Project onto the window
        a = max(ds, window_start)
        b = min(de, window_end)
        if a >= b:
            continue
        for i in range(a - window_start, b - window_start):
            if 0 <= i < len(chars):
                chars[i] = " "
    return "".join(chars)


def _strip_tags(text: str) -> str:
    """Remove every citation tag (primary + fallbacks) from the text.

    We strip in priority order so longer matches go first (avoids leaving
    stray brackets). Trailing whitespace before the tag is preserved so
    sentence spacing isn't doubled."""
    out = _PRIMARY.sub("", text)
    out = _FALLBACK_BRACKETED.sub("", out)
    # Numeric fallback strip is risky — could remove "[1995]" from prose.
    # Skip it for the clean path; verifier already reads from the raw text.
    return _normalize_whitespace(out)


def _render_html(text: str, citations: dict[int, Citation]) -> str:
    """Render citation tags as inline footnote markup.

    Each tag becomes `<sup><a href="#cite-N">[N]</a></sup>`. Failed
    citations get a 'cite-failed' class so the UI can grey them out.
    Other text is HTML-escaped to prevent XSS via injected document content.
    """
    parts: list[str] = []
    last_end = 0
    for m in _PRIMARY.finditer(text):
        parts.append(html.escape(text[last_end:m.start()]))
        cid = int(m.group(1))
        cit = citations.get(cid)
        cls = "cite"
        if cit is None or cit.status == "fabricated":
            cls = "cite cite-failed"
        elif cit.status in {"unverified", "weak"}:
            cls = "cite cite-warn"
        parts.append(
            f'<sup class="{cls}"><a href="#cite-{cid}">[{cid}]</a></sup>'
        )
        last_end = m.end()
    parts.append(html.escape(text[last_end:]))
    out = "".join(parts)
    # Bracketed fallbacks render as plain text — no superscript styling.
    # The fallback_pattern_used flag tells the UI to nudge users toward a
    # better-adhering model.
    return out


_END_DOTTED_TWO_LETTER = re.compile(r"\b[a-z]\.[a-z]\.$")    # i.e., e.g., U.S.
_END_SINGLE_INITIAL    = re.compile(r"(?:^|\s)[a-z]\.$")    # ` J.` at end


def _ends_in_abbreviation(fragment: str) -> bool:
    """Return True if `fragment` ends with text that the regex splitter
    treated as a sentence boundary but is really an abbreviation embedded
    in a longer sentence.

    Looks at the END of the fragment only (so "Mr. Adams and Mrs." is
    detected via the trailing "Mrs.", not via the leading "Mr."). This
    is what enables iterative buffering: as we accumulate fragments, we
    keep merging until the buffer stops ending in an abbreviation.

    Heuristics applied to the trailing portion:
      - Two-letter dotted abbrev (`e.g.`, `i.e.`, `U.S.`)
      - Single-letter initial (`J.`)
      - Last whitespace-separated word matches the abbreviation set
    """
    s = fragment.rstrip()
    if not s.endswith("."):
        return False
    s_lower = s.lower()
    if _END_DOTTED_TWO_LETTER.search(s_lower):
        return True
    if _END_SINGLE_INITIAL.search(s_lower):
        return True
    last = s_lower.split()[-1].rstrip(".")
    if last in _ABBREV_TITLE:
        return True
    return False


def _split_into_claims(text: str) -> list[Claim]:
    """Split the answer into sentence-shaped claims and tag each with the
    citation ids that appear inside it.

    Sentence boundaries are heuristic (period+space+capital). Buffer-merge
    keeps accumulating fragments until the buffer no longer ENDS in an
    abbreviation, so "Mr. Adams and Mrs. Brown met" stays one claim and
    "J. R. R. Tolkien wrote LOTR" stays one claim.
    """
    raw_parts = _SENTENCE_SPLIT.split(text)
    merged_parts: list[str] = []
    buffer: str = ""
    for part in raw_parts:
        buffer = (buffer.rstrip() + " " + part).strip() if buffer else part
        if _ends_in_abbreviation(buffer):
            # Keep accumulating; the next sentence-fragment is really
            # part of this one.
            continue
        merged_parts.append(buffer)
        buffer = ""
    if buffer:
        # Trailing abbreviation with no following sentence — keep as-is.
        merged_parts.append(buffer)

    claims: list[Claim] = []
    for sent in merged_parts:
        if not sent.strip():
            continue
        # Collect citation ids from within this sentence (primary first,
        # fallbacks fill in if no primary matched within).
        ids: list[int] = []
        for m in _PRIMARY.finditer(sent):
            ids.append(int(m.group(1)))
        for m in _FALLBACK_BRACKETED.finditer(sent):
            ids.append(int(m.group(1)))
        # Numeric fallback only contributes if no primary/bracketed found
        # in this sentence (avoids treating "[2023]" as a citation when
        # citations are clearly being expressed as <c> tags elsewhere).
        if not ids:
            for m in _FALLBACK_NUMERIC.finditer(sent):
                cid = int(m.group(1))
                if 1 <= cid <= 999:
                    ids.append(cid)
        # Dedup within a single sentence — `<c id="1"/><c id="1"/>` already
        # collapsed by _collapse_consecutive_duplicates, but a sentence
        # mentioning the same source twice (e.g., in a list within one
        # sentence) shouldn't show double-cite either.
        deduped: list[int] = []
        seen: set[int] = set()
        for cid in ids:
            if cid in seen:
                continue
            seen.add(cid)
            deduped.append(cid)
        cleaned_sentence = _strip_tags(sent).strip()
        if not cleaned_sentence:
            continue
        claims.append(Claim(text=cleaned_sentence, citation_ids=deduped))
    return claims


def _render_terminal(text: str) -> str:
    """Render citation tags as bracketed `[N]` markers for terminal display.

    Plain ASCII — works in every terminal regardless of font. Status-aware
    coloring lives in `render_terminal_with_ansi` (separate function so
    log-style consumers can use plain output)."""
    out = _PRIMARY.sub(lambda m: f"[{int(m.group(1))}]", text)
    # Bracketed-form fallbacks already look like `[c:1]` / `[1]`. Normalize
    # the verbose forms to the bare numeric form so terminal output is
    # consistent regardless of which pattern the LLM used.
    out = _FALLBACK_BRACKETED.sub(lambda m: f"[{int(m.group(1))}]", out)
    return _normalize_whitespace(out)


def render_terminal_with_ansi(
    answer: SynthesizedAnswer,
    *,
    use_color: bool = True,
    show_references: bool = True,
) -> str:
    """Render the answer for a terminal, with optional ANSI coloring.

    Verified citations: default color
    Unverified / weak:  yellow (warning)
    Fabricated:         dim grey (failed)

    set use_color=False for log-style output (no escapes). show_references
    appends a numbered list of cited sources after the answer (academic-
    paper style).
    """
    DIM    = "\x1b[2m"  if use_color else ""
    YELLOW = "\x1b[33m" if use_color else ""
    RESET  = "\x1b[0m"  if use_color else ""

    def _marker(cid: int) -> str:
        cit = answer.citations.get(cid)
        body = f"[{cid}]"
        if cit is None or cit.status == "fabricated":
            return f"{DIM}{body}{RESET}"
        if cit.status in {"unverified", "weak"}:
            return f"{YELLOW}{body}{RESET}"
        return body

    # Walk answer_raw and substitute each tag with its colored marker.
    parts: list[str] = []
    last_end = 0
    for m in _PRIMARY.finditer(answer.answer_raw):
        parts.append(answer.answer_raw[last_end:m.start()])
        parts.append(_marker(int(m.group(1))))
        last_end = m.end()
    parts.append(answer.answer_raw[last_end:])
    body = _normalize_whitespace("".join(parts))

    if not show_references or not answer.citations:
        return body

    refs: list[str] = ["", "Sources:"]
    for cid in sorted(answer.citations.keys()):
        cit = answer.citations[cid]
        if cit.status == "fabricated":
            refs.append(f"  {DIM}[{cid}] (fabricated — not in retrieval result){RESET}")
            continue
        status_tag = ""
        if cit.status == "weak":
            status_tag = f" {YELLOW}(semantic match only){RESET}"
        elif cit.status == "unverified":
            status_tag = f" {YELLOW}(unverified — low cosine){RESET}"
        # Compose attribution: "doc_title — chunk_label" (truncate to 80
        # chars total). Both fields are part of the lean envelope.
        title = cit.doc_title or ""
        label = cit.chunk_label or ""
        if title and label:
            attribution = f"{title} — {label}"
        else:
            attribution = title or label
        if len(attribution) > 90:
            attribution = attribution[:87] + "…"
        refs.append(f"  [{cid}]{status_tag} {attribution}")
    return body + "\n" + "\n".join(refs)


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces created by tag-stripping. Don't touch newlines
    so paragraph structure survives."""
    return re.sub(r"[ \t]{2,}", " ", text)


def _first_n_chars(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"
