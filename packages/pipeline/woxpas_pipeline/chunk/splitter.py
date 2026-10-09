"""Block → chunk splitter (the retrieval-unit builder).

Design (locked with the user):
  - Aggregate blocks in ordinal order. Never split a block, except as a fallback
    when a single block exceeds hard_max_tokens (then split at sentence boundaries).
  - No overlap between chunks. Eliminates the duplicate-extraction problem the
    user hit with their previous chunker. Extraction reads canonical_blocks
    directly, not chunks; chunks are pure retrieval artefacts.
  - Spans are sentence-level. One span per sentence (or one span per block-cell
    when a block has no detectable sentences — e.g., a table row).
  - Token count estimated as char_count / 4.0. Avoids loading a tokenizer just
    for budgeting; the embedder's actual tokenizer handles real splits.

The functions here are pure: they read a list of block dicts and return chunk
+ span structures. Persistence lives in the stage handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from woxpas_core import Span
from woxpas_core.chunk import Chunk
from woxpas_core.locator import (
    AudioLocator,
    EmailLocator,
    PdfLocator,
    SourceLocator,
    TextLocator,
)

from woxpas_pipeline.chunk.segmenter import Sentence, segment

CHARS_PER_TOKEN_ESTIMATE = 4.0


def estimate_tokens(text: str) -> int:
    """Char-count divided by ~4. Fast, no tokenizer load. Conservative for
    English; may slightly underestimate for languages with longer Unicode
    code-units, but the embedder's real tokenizer is the safety net."""
    return max(1, int(len(text) / CHARS_PER_TOKEN_ESTIMATE))


@dataclass(slots=True)
class _BlockSlice:
    """A block (or sub-piece of a too-large block) ready for packing."""
    block_id: UUID
    text: str
    type: str
    level: int | None
    source_locator: dict[str, Any]
    sentences: list[Sentence] = field(default_factory=list)


@dataclass(slots=True)
class _BuiltChunk:
    """In-memory chunk before persistence."""
    ordinal: int
    text: str
    block_ids: list[UUID]
    source_locator: dict[str, Any]
    spans: list[_BuiltSpan] = field(default_factory=list)
    token_count: int = 0


@dataclass(slots=True)
class _BuiltSpan:
    """In-memory span before persistence."""
    text: str
    block_id: UUID
    offset_in_chunk: tuple[int, int]
    source_locator: dict[str, Any]


def build_chunks(
    blocks: list[dict[str, Any]],
    *,
    target_tokens: int,
    hard_max_tokens: int,
    min_tokens: int,
    language: str | None = None,
) -> list[_BuiltChunk]:
    """Walk blocks in order, aggregate into chunks, generate spans per sentence."""
    if not blocks:
        return []

    slices = _materialize_slices(blocks, hard_max_tokens, language)

    chunks: list[_BuiltChunk] = []
    current: _BuiltChunk | None = None

    for sl in slices:
        sl_tokens = estimate_tokens(sl.text)

        # Flush current if adding this slice would exceed hard_max.
        if current is not None and (current.token_count + sl_tokens) > hard_max_tokens:
            chunks.append(current)
            current = None

        if current is None:
            current = _BuiltChunk(
                ordinal=len(chunks),
                text="",
                block_ids=[],
                source_locator={"kind": "aggregate", "parts": []},
            )

        _append_slice(current, sl)
        current.token_count = estimate_tokens(current.text)

        # Greedy close at target — keeps chunks near the sweet spot rather than
        # always packing to hard_max.
        if current.token_count >= target_tokens:
            chunks.append(current)
            current = None

    if current is not None:
        # Coalesce a tiny trailing chunk into the previous one if it'd be too small.
        if chunks and current.token_count < min_tokens \
                and (chunks[-1].token_count + current.token_count) <= hard_max_tokens:
            _merge_chunks(chunks[-1], current)
        else:
            chunks.append(current)

    return chunks


# ─── internals ──────────────────────────────────────────────────────────────

def _materialize_slices(
    blocks: list[dict[str, Any]],
    hard_max_tokens: int,
    language: str | None,
) -> list[_BlockSlice]:
    """Convert raw block rows to _BlockSlice list, splitting any block that's
    bigger than hard_max into sentence-aligned sub-slices.

    Each sub-slice carries a derived source_locator that points to the right
    sub-range of the parent block (offsets adjusted within the parent's range).
    """
    out: list[_BlockSlice] = []
    for b in blocks:
        text = (b.get("text") or "").rstrip()
        if not text:
            continue

        tokens = estimate_tokens(text)
        sentences = segment(text, language=language)

        if tokens <= hard_max_tokens:
            out.append(_BlockSlice(
                block_id=b["block_id"],
                text=text,
                type=str(b["type"]),
                level=b.get("level"),
                source_locator=dict(b["source_locator"]),
                sentences=sentences,
            ))
            continue

        # Too-large block — split at sentence boundaries.
        out.extend(_split_oversized(b, sentences, hard_max_tokens, text))
    return out


def _split_oversized(
    block: dict[str, Any], sentences: list[Sentence],
    hard_max_tokens: int, text: str,
) -> list[_BlockSlice]:
    """Split a single block into sentence-aligned pieces under hard_max."""
    if not sentences:
        # No detectable sentences (e.g., a code block) — split arbitrarily by char count.
        max_chars = int(hard_max_tokens * CHARS_PER_TOKEN_ESTIMATE)
        pieces: list[_BlockSlice] = []
        cursor = 0
        while cursor < len(text):
            stop = min(cursor + max_chars, len(text))
            piece_text = text[cursor:stop]
            pieces.append(_BlockSlice(
                block_id=block["block_id"],
                text=piece_text,
                type=str(block["type"]),
                level=block.get("level"),
                source_locator=_offset_locator(
                    block["source_locator"], char_start_delta=cursor,
                    char_len=len(piece_text),
                ),
            ))
            cursor = stop
        return pieces

    out: list[_BlockSlice] = []
    bucket: list[Sentence] = []
    bucket_chars = 0
    for sent in sentences:
        sent_chars = sent.end - sent.start
        if bucket and (bucket_chars + sent_chars) > int(hard_max_tokens * CHARS_PER_TOKEN_ESTIMATE):
            out.append(_slice_from_sentences(block, bucket, text))
            bucket, bucket_chars = [], 0
        bucket.append(sent)
        bucket_chars += sent_chars
    if bucket:
        out.append(_slice_from_sentences(block, bucket, text))
    return out


def _slice_from_sentences(
    block: dict[str, Any], sentences: list[Sentence], full_text: str,
) -> _BlockSlice:
    """Build a _BlockSlice from a contiguous run of sentences."""
    start = sentences[0].start
    end   = sentences[-1].end
    text  = full_text[start:end]
    # Re-derive sentence offsets relative to this slice's text.
    rebased = [
        Sentence(text=s.text, start=s.start - start, end=s.end - start)
        for s in sentences
    ]
    return _BlockSlice(
        block_id=block["block_id"],
        text=text,
        type=str(block["type"]),
        level=block.get("level"),
        source_locator=_offset_locator(
            block["source_locator"], char_start_delta=start, char_len=len(text),
        ),
        sentences=rebased,
    )


def _offset_locator(
    parent_locator: dict[str, Any] | SourceLocator,
    *,
    char_start_delta: int, char_len: int,
) -> dict[str, Any]:
    """Adjust a parent block's source_locator to a sub-range within it.

    Handles the discriminated-union locator types. If the locator kind doesn't
    have natural char offsets (e.g., audio segment), we leave it as-is and rely
    on the chunk text alone for that subset.
    """
    loc = parent_locator if isinstance(parent_locator, dict) else parent_locator.model_dump(mode="json")
    kind = loc.get("kind")
    if kind == "pdf":
        new = dict(loc)
        cs = int(new.get("char_start_in_page", 0)) + char_start_delta
        new["char_start_in_page"] = cs
        new["char_end_in_page"]   = cs + char_len
        return new
    if kind == "text":
        new = dict(loc)
        cs = int(new.get("char_start", 0)) + char_start_delta
        new["char_start"] = cs
        new["char_end"]   = cs + char_len
        return new
    if kind == "email":
        new = dict(loc)
        cs = int(new.get("char_start", 0)) + char_start_delta
        new["char_start"] = cs
        new["char_end"]   = cs + char_len
        return new
    return loc           # audio / image / unknown — leave intact


def _append_slice(chunk: _BuiltChunk, sl: _BlockSlice) -> None:
    """Append a slice to a chunk, generating spans + tracking source provenance."""
    join = "\n\n" if chunk.text else ""
    chunk_offset = len(chunk.text) + len(join)
    chunk.text += join + sl.text
    chunk.block_ids.append(sl.block_id)

    # Spans: one per sentence in the slice. Offsets are relative to chunk.text.
    if sl.sentences:
        for sent in sl.sentences:
            start = chunk_offset + sent.start
            end   = chunk_offset + sent.end
            chunk.spans.append(_BuiltSpan(
                text=sent.text,
                block_id=sl.block_id,
                offset_in_chunk=(start, end),
                source_locator=_locator_for_span(sl, sent),
            ))
    else:
        # No detectable sentences (table rows, code, very short blocks) — one
        # span covering the whole slice. Better than zero spans for citation.
        chunk.spans.append(_BuiltSpan(
            text=sl.text,
            block_id=sl.block_id,
            offset_in_chunk=(chunk_offset, chunk_offset + len(sl.text)),
            source_locator=sl.source_locator,
        ))

    # Track which blocks contributed to this chunk's source_locator aggregate.
    chunk.source_locator.setdefault("parts", []).append({
        "block_id": str(sl.block_id),
        "type": sl.type,
        "locator": sl.source_locator,
    })


def _locator_for_span(sl: _BlockSlice, sent: Sentence) -> dict[str, Any]:
    """Produce a sentence-level source locator within the slice's parent block."""
    return _offset_locator(
        sl.source_locator,
        char_start_delta=sent.start,
        char_len=sent.end - sent.start,
    )


def _merge_chunks(target: _BuiltChunk, tail: _BuiltChunk) -> None:
    """Coalesce a too-small tail chunk into its predecessor."""
    join = "\n\n"
    base_off = len(target.text) + len(join)
    target.text += join + tail.text
    target.block_ids.extend(tail.block_ids)
    target.token_count = estimate_tokens(target.text)
    target.source_locator.setdefault("parts", []).extend(
        tail.source_locator.get("parts", [])
    )
    for sp in tail.spans:
        target.spans.append(_BuiltSpan(
            text=sp.text, block_id=sp.block_id,
            offset_in_chunk=(sp.offset_in_chunk[0] + base_off,
                             sp.offset_in_chunk[1] + base_off),
            source_locator=sp.source_locator,
        ))


# ─── conversion to core types (with content-derived IDs) ────────────────────

def to_core_chunks_and_spans(
    built: list[_BuiltChunk],
    *,
    doc_id: UUID,
    tenant_id: UUID,
    embed_model: str,
    embed_model_version: str,
    processing_profile: str,
    pipeline_version: str,
    valid_time_start: Any | None,
    valid_time_end: Any | None,
    scope_ids: list[UUID],
) -> tuple[list[Chunk], list[Span]]:
    """Convert in-memory chunks to core Chunk + Span dataclasses with stable
    content-derived IDs.

    qdrant_point_id is the chunk_id for v0.1 (1:1 mapping). The embed/index
    stage upserts the vector under that point id.
    """
    chunks: list[Chunk] = []
    spans:  list[Span]  = []
    for bc in built:
        chunk_id = Chunk.derive_id(doc_id, bc.ordinal, bc.text, embed_model_version)
        chunk = Chunk(
            chunk_id=chunk_id,
            doc_id=doc_id,
            tenant_id=tenant_id,
            ordinal=bc.ordinal,
            text=bc.text,
            token_count=bc.token_count,
            block_ids=bc.block_ids,
            source_locator=bc.source_locator,
            qdrant_point_id=chunk_id,
            embed_model=embed_model,
            embed_model_version=embed_model_version,
            scope_ids=scope_ids,
            processing_profile=processing_profile,
            valid_time_start=valid_time_start,
            valid_time_end=valid_time_end,
            pipeline_version=pipeline_version,
        )
        chunks.append(chunk)

        for ord_, bs in enumerate(bc.spans):
            # Coerce dict locator to a typed SourceLocator for Span.make.
            loc = _coerce_locator(bs.source_locator)
            span = Span.make(
                chunk_id=chunk_id,
                doc_id=doc_id,
                ordinal=ord_,
                text=bs.text,
                offset_in_chunk=bs.offset_in_chunk,
                source_locator=loc,
                pipeline_version=pipeline_version,
                block_id=bs.block_id,
            )
            spans.append(span)

    return chunks, spans


def _coerce_locator(loc: dict[str, Any]) -> SourceLocator:
    """Best-effort cast from dict back to a typed SourceLocator union member."""
    kind = loc.get("kind")
    if kind == "pdf":
        return PdfLocator.model_validate(loc)
    if kind == "text":
        return TextLocator.model_validate(loc)
    if kind == "audio":
        return AudioLocator.model_validate(loc)
    if kind == "email":
        return EmailLocator.model_validate(loc)
    # Fallback: synthesize a TextLocator with zero offsets so downstream consumers
    # get a valid discriminated value rather than a crash.
    return TextLocator(char_start=0, char_end=0)
