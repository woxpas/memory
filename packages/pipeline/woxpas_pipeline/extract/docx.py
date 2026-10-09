"""DOCX extractor (python-docx, MIT).

Walks the docx in document order — paragraphs and tables — preserving:
  - heading levels via paragraph style ("Heading 1" .. "Heading 9") → BlockType.HEADING
  - list items via paragraph style ("List Bullet*" / "List Number*" / numPr) → BlockType.LIST_ITEM
  - regular paragraphs → BlockType.PARAGRAPH
  - tables → one BlockType.TABLE_ROW per row

Source locator is TextLocator with section_path + a synthetic char range derived
from the document-order index. python-docx doesn't surface true byte offsets;
the synthetic ranges are stable per (doc_id, ordinal) and let the chunker /
spans system address blocks consistently. UI will render the original .docx via
its own component when needed (offsets here aren't byte-accurate to the .docx
binary).

We do NOT extract images, embedded objects, comments, or footnotes in v0.1 —
they're noted in the metadata when present so a later extractor pass can pick
them up if the user wants.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
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

_HEADING_STYLE_PREFIX = "Heading "      # "Heading 1", "Heading 2", ...
_LIST_BULLET_PREFIX   = "List Bullet"
_LIST_NUMBER_PREFIX   = "List Number"
_LIST_PARAGRAPH       = "List Paragraph"


class DocxExtractor(Extractor):
    name = "docx"
    extractor_version = "0.1.0"
    supported_mimes = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    def extract(self, inp: ExtractInput) -> ExtractResult:
        doc = Document(BytesIO(inp.content))
        blocks: list[Block] = []
        section_path: list[str] = []
        char_cursor = 0
        skipped_assets = {"images": 0, "footnotes": 0, "comments": 0}

        # core_properties carries doc-level metadata
        core = doc.core_properties

        # Walk top-level body elements in document order. python-docx exposes
        # paragraphs and tables; we iterate the underlying body to keep order.
        for child in doc.element.body.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                para = Paragraph(child, doc)
                if not para.text.strip():
                    continue

                # Image-bearing paragraphs: count and skip in v0.1
                if _has_inline_image(para):
                    skipped_assets["images"] += 1
                    # still extract the text portion
                    if not para.text.strip():
                        continue

                style_name = (para.style.name if para.style else "") or ""
                text = para.text
                start = char_cursor
                end   = start + len(text)
                char_cursor = end + 1   # +1 for an implicit newline between blocks

                if style_name.startswith(_HEADING_STYLE_PREFIX):
                    try:
                        level = int(style_name[len(_HEADING_STYLE_PREFIX):])
                    except ValueError:
                        level = 1
                    while len(section_path) >= level:
                        section_path.pop()
                    section_path.append(text)
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.HEADING,
                        text=text, level=level,
                        source_locator=TextLocator(
                            section_path=list(section_path),
                            char_start=start, char_end=end,
                        ),
                    ))
                    continue

                if (style_name.startswith(_LIST_BULLET_PREFIX)
                        or style_name.startswith(_LIST_NUMBER_PREFIX)
                        or style_name == _LIST_PARAGRAPH
                        or _has_numpr(para)):
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.LIST_ITEM,
                        text=text,
                        level=_list_level(para),
                        source_locator=TextLocator(
                            section_path=list(section_path),
                            char_start=start, char_end=end,
                        ),
                    ))
                    continue

                blocks.append(Block.make(
                    doc_id=inp.doc_id,
                    ordinal=len(blocks),
                    type_=BlockType.PARAGRAPH,
                    text=text,
                    source_locator=TextLocator(
                        section_path=list(section_path),
                        char_start=start, char_end=end,
                    ),
                ))

            elif tag == qn("w:tbl"):
                table = Table(child, doc)
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if not any(cells):
                        continue
                    text = " | ".join(cells)
                    start = char_cursor
                    end   = start + len(text)
                    char_cursor = end + 1
                    blocks.append(Block.make(
                        doc_id=inp.doc_id,
                        ordinal=len(blocks),
                        type_=BlockType.TABLE_ROW,
                        text=text,
                        source_locator=TextLocator(
                            section_path=list(section_path),
                            char_start=start, char_end=end,
                        ),
                        attrs={"cells": cells},
                    ))

        title = inp.declared_title or (core.title or None)
        if not title and blocks:
            for b in blocks:
                if b.type is BlockType.HEADING and b.text:
                    title = b.text
                    break

        author = core.author if core.author else None
        created = core.created
        modified = core.modified

        cdoc = CanonicalDoc(
            doc_id=inp.doc_id,
            tenant_id=inp.tenant_id,
            pipeline_version=inp.pipeline_version,
            blocks=blocks,
            doc_metadata=DocMetadata(
                title=title,
                author=author,
                created_at_in_doc=created,
                modified_at_in_doc=modified,
                language=inp.declared_language,
                source_format="docx",
                encoding="utf-8",
                extra={"skipped_assets": skipped_assets} if any(skipped_assets.values()) else {},
            ),
            structure_index=StructureIndex(
                headings_tree=[
                    {"block_id": str(b.block_id), "level": b.level or 1,
                     "text": b.text or "", "ordinal": b.ordinal}
                    for b in blocks if b.type is BlockType.HEADING
                ],
            ),
            extractor=self.name,
            extractor_version=self.extractor_version,
            extracted_at=datetime.now(UTC),
        )
        return ExtractResult(
            canonical_doc=cdoc,
            metrics=ExtractMetrics(blocks=len(blocks), chars=char_cursor,
                                   extra={"skipped_assets": skipped_assets}),
        )


# ─── helpers ────────────────────────────────────────────────────────────────

def _has_inline_image(para: Paragraph) -> bool:
    """python-docx puts inline images inside <w:r><w:drawing>…</w:drawing></w:r>."""
    return bool(para._element.findall(".//" + qn("w:drawing")))


def _has_numpr(para: Paragraph) -> bool:
    """Numbering properties: <w:pPr><w:numPr>…</w:numPr></w:pPr> indicates a list paragraph."""
    pPr = para._element.find(qn("w:pPr"))
    if pPr is None:
        return False
    return pPr.find(qn("w:numPr")) is not None


def _list_level(para: Paragraph) -> int:
    """Read <w:numPr><w:ilvl w:val='N'/></w:numPr> when present."""
    pPr = para._element.find(qn("w:pPr"))
    if pPr is None:
        return 0
    numPr = pPr.find(qn("w:numPr"))
    if numPr is None:
        return 0
    ilvl = numPr.find(qn("w:ilvl"))
    if ilvl is None:
        return 0
    val = ilvl.get(qn("w:val"))
    try:
        return int(val) if val else 0
    except ValueError:
        return 0
