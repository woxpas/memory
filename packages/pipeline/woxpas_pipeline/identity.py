"""Identity-seed markdown parser.

Parses the structured markdown that comes out of the identity-export prompt
template (see templates/identity_export_prompt.md) into a list of fact rows
suitable for direct insert into entity_profile_facts.

v0.1 deliberately uses a simple regex parser — no LLM. The format is
human-pasted, the structure is fixed (## Heading + bullets), and the parser
is deterministic so re-imports converge. Phase 2 extraction can re-process
the same source markdown via doc upload (processing_profile=identity_seed)
to refine fact_kind / fact_value structures with LLM judgment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Markdown H2 heading → fact_category. Anything not in the map collapses
# to 'other'. Categories are matched case-insensitively, with normalized
# whitespace.
_HEADING_TO_CATEGORY: dict[str, str] = {
    "bio":                  "bio",
    "profession":           "profession",
    "professional":         "profession",
    "communication style":  "comm_style",
    "comm style":           "comm_style",
    "communication":        "comm_style",
    "projects":             "projects",
    "preferences":          "preferences",
    "preference":           "preferences",
    "expertise":            "expertise",
    "skills":               "expertise",
    "relationships":        "relationships",
    "goals":                "goals",
    "health":               "health",
    "other":                "other",
    "misc":                 "other",
    "miscellaneous":        "other",
}

_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_KV_RE = re.compile(r"^([A-Za-z][\w\s-]{0,38}):\s+(.+)$")


@dataclass(slots=True)
class IdentityFact:
    """One row destined for entity_profile_facts."""
    fact_category: str
    fact_kind: str
    fact_value: dict[str, Any]


def parse_identity_markdown(text: str) -> list[IdentityFact]:
    """Walk H2 sections + their bullets, emit one IdentityFact per bullet.

    Recognized:
      ## Heading           → switches fact_category for subsequent bullets
      - bullet text        → one fact (also '*' or '+')
      - Key: Value bullet  → fact_kind from the key, fact_value.text from value

    Headings outside the recognized map fold into 'other'. Bullets outside
    any heading are skipped (defensive — typical for prompt preambles).
    """
    facts: list[IdentityFact] = []
    current_category: str | None = None

    for raw_line in text.splitlines():
        # Heading: open a new section.
        h = _HEADING_RE.match(raw_line)
        if h:
            heading_text = h.group(1).strip().lower()
            current_category = _HEADING_TO_CATEGORY.get(heading_text, "other")
            continue

        # Skip bullets that arrive before any section is opened.
        if current_category is None:
            continue

        m = _BULLET_RE.match(raw_line)
        if not m:
            continue

        content = m.group(1).strip()
        if not content:
            continue

        # Try Key: Value parsing. Restrict the key to short, mostly-alpha
        # forms so we don't accidentally split sentences with colons.
        kv = _KV_RE.match(content)
        if kv:
            kind = kv.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            value = kv.group(2).strip()
        else:
            kind = "general"
            value = content

        facts.append(IdentityFact(
            fact_category=current_category,
            fact_kind=kind,
            fact_value={"text": value},
        ))

    return facts
