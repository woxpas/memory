"""Extractor registry — maps MIMEs to Extractor instances.

Adding a new extractor: write the class, instantiate, register. Idempotent;
last-registered wins for a given MIME (good for monkey-patches in tests, also
good for "newer extractor version" upgrades).
"""

from __future__ import annotations

import structlog

from woxpas_pipeline.extract.base import Extractor

log = structlog.get_logger(__name__)


class ExtractorRegistry:
    def __init__(self) -> None:
        self._by_mime: dict[str, Extractor] = {}

    def register(self, extractor: Extractor) -> None:
        for mime in extractor.supported_mimes:
            self._by_mime[mime] = extractor
        log.info("extract.registry.registered",
                 name=extractor.name, mimes=list(extractor.supported_mimes))

    def for_mime(self, mime: str) -> Extractor | None:
        # Exact-match first; fall back to lenient prefix (e.g., "text/plain; charset=utf-8")
        if mime in self._by_mime:
            return self._by_mime[mime]
        prefix = mime.split(";", 1)[0].strip()
        return self._by_mime.get(prefix)

    def known_mimes(self) -> list[str]:
        return sorted(self._by_mime)


def build_default_registry() -> ExtractorRegistry:
    """Build the registry with the v1 extractor set."""
    from woxpas_pipeline.extract.audio import AudioExtractor
    from woxpas_pipeline.extract.docx import DocxExtractor
    from woxpas_pipeline.extract.pdf import PdfExtractor
    from woxpas_pipeline.extract.text import TextExtractor

    reg = ExtractorRegistry()
    reg.register(TextExtractor())
    reg.register(PdfExtractor())
    reg.register(DocxExtractor())
    reg.register(AudioExtractor())
    return reg
