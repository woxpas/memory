"""Text + Markdown extractor.

Plain text and Markdown become a sequence of paragraph blocks split on blank lines.
Markdown headings (# / ## / ### / ####) become heading blocks with the right level.
Lists are detected line-by-line as list_item blocks (level via leading spaces / 2).

Char offsets in source_locator are absolute over the decoded text — that's what
the UI uses to highlight in the original markdown view.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from woxpas_core import (
    Block,
    BlockType,
    CanonicalDoc,
    DocMetadata,
    StructureIndex,
    TextLocator,
)

from woxpas_pipeline.extract.base import (
    ExtractInput,
    ExtractMetrics,
    Extractor,
    ExtractResult,
)

# Heading: ^#{1,6} text (Markdown)
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# Unordered list: -, *, +
_RE_ULIST   = re.compile(r"^(\s*)[-*+]\s+(.+?)\s*$")
# Ordered list:   1. or 1)
_RE_OLIST   = re.compile(r"^(\s*)\d+[.)]\s+(.+?)\s*$")


class TextExtractor(Extractor):
    name = "text"
    extractor_version = "0.1.0"
    supported_mimes = (
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    )

    def extract(self, inp: ExtractInput) -> ExtractResult:
        text = self._decode(inp.content)
        is_md = inp.mime != "text/plain"

        blocks: list[Block] = []
        section_path: list[str] = []
        ordinal = 0
        cursor = 0       # char offset into source

        # Walk by lines, group into blocks separated by blank lines.
        lines = text.splitlines(keepends=True)
        para_buf: list[str] = []
        para_start: int | None = None

        def flush_paragraph(end: int) -> int:
            nonlocal ordinal
            if para_buf and para_start is not None:
                joined = "".join(para_buf).strip()
                if joined:
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.PARAGRAPH,
                        text=joined,
                        source_locator=TextLocator(
                            section_path=list(section_path),
                            char_start=para_start,
                            char_end=end,
                        ),
                    ))
            para_buf.clear()
            return end

        for line in lines:
            line_start = cursor
            cursor += len(line)
            stripped = line.rstrip("\n").rstrip()

            if not stripped:
                flush_paragraph(line_start)
                continue

            # Markdown heading
            if is_md:
                m = _RE_HEADING.match(stripped)
                if m:
                    flush_paragraph(line_start)
                    level = len(m.group(1))
                    title = m.group(2)
                    # Maintain a section path for downstream blocks.
                    while len(section_path) >= level:
                        section_path.pop()
                    section_path.append(title)
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.HEADING,
                        text=title,
                        level=level,
                        source_locator=TextLocator(
                            section_path=list(section_path),
                            char_start=line_start,
                            char_end=cursor,
                        ),
                    ))
                    continue

                # List items (preserve nesting via leading-spaces / 2 as level)
                m = _RE_ULIST.match(line.rstrip("\n"))
                if m is None:
                    m = _RE_OLIST.match(line.rstrip("\n"))
                if m:
                    flush_paragraph(line_start)
                    indent = len(m.group(1))
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.LIST_ITEM,
                        text=m.group(2),
                        level=indent // 2,
                        source_locator=TextLocator(
                            section_path=list(section_path),
                            char_start=line_start,
                            char_end=cursor,
                        ),
                    ))
                    continue

            # Default: accumulate into the current paragraph buffer.
            if para_start is None:
                para_start = line_start
            para_buf.append(line)

        flush_paragraph(cursor)

        # Title: first heading wins; else first non-empty line truncated.
        title: str | None = inp.declared_title
        if title is None and blocks:
            for b in blocks:
                if b.type is BlockType.HEADING and b.text:
                    title = b.text
                    break
            if title is None:
                title = (text.strip().splitlines() or [""])[0][:120] or None

        cdoc = CanonicalDoc(
            doc_id=inp.doc_id,
            tenant_id=inp.tenant_id,
            pipeline_version=inp.pipeline_version,
            blocks=blocks,
            doc_metadata=DocMetadata(
                title=title,
                language=inp.declared_language,
                source_format=inp.mime,
                encoding="utf-8",
            ),
            structure_index=StructureIndex(
                headings_tree=self._headings_tree(blocks),
            ),
            extractor=self.name,
            extractor_version=self.extractor_version,
            extracted_at=datetime.now(UTC),
        )
        return ExtractResult(
            canonical_doc=cdoc,
            metrics=ExtractMetrics(blocks=len(blocks), chars=len(text)),
        )

    @staticmethod
    def _decode(content: bytes) -> str:
        # Try utf-8 then latin-1 as a permissive fallback.
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="replace")

    @staticmethod
    def _headings_tree(blocks: list[Block]) -> list[dict]:
        out: list[dict] = []
        for b in blocks:
            if b.type is BlockType.HEADING:
                out.append({
                    "block_id": str(b.block_id),
                    "level": b.level or 1,
                    "text": b.text or "",
                    "ordinal": b.ordinal,
                })
        return out
