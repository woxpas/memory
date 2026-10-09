"""Source locators — discriminated union pinpointing where in the original a
block/chunk/span lives.

Every locator must let the UI navigate the user back to the exact origin:
- pdf:    page + char range + bbox to draw a rectangle on rendered page
- text:   section path + char range in canonical text
- audio:  timestamp range + segment id to seek a waveform
- email:  mime part + char range
- image:  bbox in the rendered page (future)
- video:  timestamp + frame + bbox (future)

Adding a new kind = additive change, never a breaking one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class PdfLocator(BaseModel):
    kind: Literal["pdf"] = "pdf"
    page: int = Field(ge=1)
    char_start_in_page: int = Field(ge=0)
    char_end_in_page: int = Field(ge=0)
    bbox: tuple[float, float, float, float] | None = None
    """Page-relative bbox: (x1, y1, x2, y2) in PDF user space units."""


class TextLocator(BaseModel):
    kind: Literal["text"] = "text"
    section_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)


class AudioLocator(BaseModel):
    kind: Literal["audio"] = "audio"
    segment_id: str
    t_start_ms: int = Field(ge=0)
    t_end_ms: int = Field(ge=0)
    speaker: str | None = None


class EmailLocator(BaseModel):
    kind: Literal["email"] = "email"
    part_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    header: str | None = None


class ImageLocator(BaseModel):
    kind: Literal["image"] = "image"
    bbox: tuple[float, float, float, float]
    page: int | None = None


SourceLocator = Annotated[
    PdfLocator | TextLocator | AudioLocator | EmailLocator | ImageLocator,
    Field(discriminator="kind"),
]


def locator_kind(loc: SourceLocator) -> str:
    """Return the discriminator string of a locator (for logs / SQL queries)."""
    return loc.kind
