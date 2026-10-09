"""Sentence segmentation via pysbd (rule-based, MIT, multilingual).

Why we need sentence boundaries:
  - Each span = one sentence (UI highlighting granularity).
  - Mid-block split fallback: when a single block exceeds chunk_hard_max, we
    split at the nearest sentence boundary, never mid-sentence.
  - Citation pinpointing: an extracted fact's span boundary must align with
    what a reader calls "the same sentence" — bad segmenters split at
    abbreviations and citations look broken.

pysbd handles ~25 languages out of the box, costs ~10ms per doc, no model file.
We instantiate one Segmenter per language as needed; the cache is process-local.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pysbd


@dataclass(slots=True, frozen=True)
class Sentence:
    """A sentence + its character offsets within the source text."""
    text: str
    start: int        # inclusive char offset in the source string
    end:   int        # exclusive


_segmenter_cache: dict[str, pysbd.Segmenter] = {}
_cache_lock = threading.Lock()


def _segmenter_for(language: str | None) -> pysbd.Segmenter:
    """Return a cached Segmenter for the given language code."""
    code = (language or "en").split("-", 1)[0].lower()
    with _cache_lock:
        seg = _segmenter_cache.get(code)
        if seg is None:
            try:
                seg = pysbd.Segmenter(language=code, clean=False, char_span=True)
            except (ValueError, KeyError):
                # Unsupported language → fall back to English (still better than regex).
                seg = pysbd.Segmenter(language="en", clean=False, char_span=True)
            _segmenter_cache[code] = seg
        return seg


def segment(text: str, language: str | None = None) -> list[Sentence]:
    """Split text into sentences with byte-accurate offsets.

    pysbd's char_span mode returns (start, end) tuples per sentence. We re-slice
    the source so the returned `text` is exactly what falls between the offsets,
    even if pysbd internally normalized whitespace.
    """
    if not text:
        return []
    seg = _segmenter_for(language)
    spans = seg.segment(text)
    out: list[Sentence] = []
    for s in spans:
        # pysbd returns objects with .sent, .start, .end attrs in char_span mode.
        start = int(s.start)
        end   = int(s.end)
        if end <= start:
            continue
        out.append(Sentence(text=text[start:end], start=start, end=end))
    return out
