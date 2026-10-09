"""PDF extractor (pdfplumber).

Why pdfplumber: MIT-licensed (closed-source-friendly), preserves per-word bbox
data we need for UI highlighting, no AGPL dependencies. PyMuPDF is faster but
AGPL — banned for this build.

Strategy:
  - For each page, use page.extract_text_lines() to get line-level text + bbox.
  - Group lines into paragraphs by blank-line gaps + indentation heuristics.
  - Each paragraph becomes a Block with PdfLocator{page, char range, bbox}.
  - char_start/char_end are within the page's extracted text (per-page coords),
    which matches what the UI needs to map back to a re-rendered page view.
  - Doc metadata (title, author, dates) is pulled from PDF's /Info dict.

For v0.1 we run the parse synchronously inside a worker thread. PDF parsing
holds the GIL minimally (most work is C); for hot paths we'll move to a process
pool in v0.2.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import structlog
from woxpas_core import (
    Block,
    BlockType,
    CanonicalDoc,
    DocMetadata,
    PdfLocator,
    StructureIndex,
)

from woxpas_pipeline.extract.base import (
    ExtractInput,
    ExtractMetrics,
    Extractor,
    ExtractResult,
)

log = structlog.get_logger(__name__)


class PdfExtractor(Extractor):
    name = "pdf"
    extractor_version = "0.1.0"
    supported_mimes = ("application/pdf",)

    def extract(self, inp: ExtractInput) -> ExtractResult:
        # Lazy import: pdfplumber is heavy; only loads when a PDF actually arrives.
        import pdfplumber  # type: ignore[import]

        blocks: list[Block] = []
        total_chars = 0
        page_count = 0
        pdf_meta: dict[str, Any] = {}

        with pdfplumber.open(io.BytesIO(inp.content)) as pdf:
            try:
                pdf_meta = pdf.metadata or {}
            except Exception:                                 # pragma: no cover
                pdf_meta = {}

            for page_no, page in enumerate(pdf.pages, start=1):
                page_count += 1
                # Lines with bboxes; tolerant of None when extraction fails.
                try:
                    lines = page.extract_text_lines(
                        layout=False, strip=True, return_chars=False,
                    ) or []
                except Exception as e:                        # pragma: no cover
                    log.warning("pdf.page_extract_failed",
                                page=page_no, error=str(e))
                    continue

                # Build per-page char-offset map by reconstructing the page text.
                # This gives us stable char_start/char_end consistent with what
                # extract_text() would return.
                page_text_parts: list[str] = []
                line_offsets: list[tuple[int, int, dict[str, Any]]] = []
                cursor = 0
                for line in lines:
                    txt = (line.get("text") or "").rstrip()
                    if not txt:
                        continue
                    start = cursor
                    page_text_parts.append(txt)
                    page_text_parts.append("\n")
                    cursor += len(txt) + 1
                    line_offsets.append((start, cursor - 1, line))

                page_text = "".join(page_text_parts)
                total_chars += len(page_text)

                # Group consecutive lines into paragraphs by vertical gap.
                paragraphs = self._group_paragraphs(line_offsets)

                for para in paragraphs:
                    text = "\n".join(o[2].get("text", "").rstrip() for o in para).strip()
                    if not text:
                        continue
                    char_start = para[0][0]
                    char_end   = para[-1][1]
                    bbox = self._bbox_union([o[2] for o in para])
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.PARAGRAPH,
                        text=text,
                        source_locator=PdfLocator(
                            page=page_no,
                            char_start_in_page=char_start,
                            char_end_in_page=char_end,
                            bbox=bbox,
                        ),
                    ))

        title = self._safe_str(pdf_meta.get("Title")) or inp.declared_title
        author = self._safe_str(pdf_meta.get("Author"))
        created = self._safe_pdf_date(pdf_meta.get("CreationDate"))
        modified = self._safe_pdf_date(pdf_meta.get("ModDate"))
        language = self._safe_str(pdf_meta.get("Language")) or inp.declared_language

        cdoc = CanonicalDoc(
            doc_id=inp.doc_id,
            tenant_id=inp.tenant_id,
            pipeline_version=inp.pipeline_version,
            blocks=blocks,
            doc_metadata=DocMetadata(
                title=title,
                author=author,
                authors=[author] if author else [],
                created_at_in_doc=created,
                modified_at_in_doc=modified,
                language=language,
                page_count=page_count,
                source_format="application/pdf",
            ),
            structure_index=StructureIndex(),       # PDFs rarely expose clean headings
            extractor=self.name,
            extractor_version=self.extractor_version,
            extracted_at=datetime.now(UTC),
        )
        return ExtractResult(
            canonical_doc=cdoc,
            metrics=ExtractMetrics(
                blocks=len(blocks), pages=page_count, chars=total_chars,
            ),
        )

    # ─── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _bbox_union(lines: list[dict[str, Any]]) -> tuple[float, float, float, float]:
        x0 = min((float(ln.get("x0", 0.0)) for ln in lines), default=0.0)
        x1 = max((float(ln.get("x1", 0.0)) for ln in lines), default=0.0)
        # pdfplumber uses 'top' / 'bottom' for y.
        y0 = min((float(ln.get("top", 0.0)) for ln in lines), default=0.0)
        y1 = max((float(ln.get("bottom", 0.0)) for ln in lines), default=0.0)
        return (x0, y0, x1, y1)

    @staticmethod
    def _group_paragraphs(
        line_offsets: list[tuple[int, int, dict[str, Any]]],
        gap_threshold: float = 6.0,
    ) -> list[list[tuple[int, int, dict[str, Any]]]]:
        """Cluster consecutive lines by vertical gap. A new paragraph starts
        whenever the vertical gap between consecutive lines exceeds the
        threshold (in PDF user-space units; 6.0 ≈ a blank line at 12pt body)."""
        groups: list[list[tuple[int, int, dict[str, Any]]]] = []
        cur: list[tuple[int, int, dict[str, Any]]] = []
        prev_bottom: float | None = None
        for entry in line_offsets:
            line = entry[2]
            top = float(line.get("top", 0.0))
            bottom = float(line.get("bottom", top))
            if cur and prev_bottom is not None and (top - prev_bottom) > gap_threshold:
                groups.append(cur)
                cur = []
            cur.append(entry)
            prev_bottom = bottom
        if cur:
            groups.append(cur)
        return groups

    @staticmethod
    def _safe_str(v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8")
            except UnicodeDecodeError:
                return v.decode("latin-1", errors="replace")
        return str(v)

    @staticmethod
    def _safe_pdf_date(v: object) -> datetime | None:
        """PDF date strings look like 'D:20240115153012+02'00''. Tolerate noise."""
        if v is None:
            return None
        s = str(v).strip()
        if s.startswith("D:"):
            s = s[2:]
        try:
            # Take the YYYYMMDDHHMMSS prefix and parse.
            return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except Exception:
            return None
