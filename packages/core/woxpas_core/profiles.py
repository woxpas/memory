"""Enumerations driving pipeline behavior."""

from enum import StrEnum


class ProcessingProfile(StrEnum):
    """Per-doc declaration of which extractors and prompts apply downstream.

    Set at ingest time (auto-classifier + user override). Drives summary prompt,
    extractor selection, and conflict-detection rules.

    Consolidated from 16 → 10 in migration 012 (see project notes). Mapping:
      thought / voice_note / mixed_note               → PERSONAL
      artefact_lyric                                  → CREATIVE_ARTEFACT
      artefact_contract                               → CONTRACT
      artefact_reference / book / paper / manual      → REFERENCE
      email / transcript / correspondence             → CORRESPONDENCE
      calendar_event / code / custom                  → unchanged
    Plus NEW: IDENTITY_SEED (markdown user-identity import) and AGENT_LOG
    (agent-authored artefacts).
    """
    PERSONAL          = "personal"          # default for thoughts, notes, voice notes
    IDENTITY_SEED     = "identity_seed"     # user identity markdown imported at init
    CREATIVE_ARTEFACT = "creative_artefact" # song lyrics, poetry — entities-only extraction
    CONTRACT          = "contract"          # legal: parties, dates, clauses, signatures
    REFERENCE         = "reference"         # external knowledge: books, papers, RFCs, manuals
    CODE              = "code"              # source code — symbols + dependencies
    CORRESPONDENCE    = "correspondence"    # email, slack threads, transcripts, multi-party comms
    CALENDAR_EVENT    = "calendar_event"    # scheduled events with time + attendees
    AGENT_LOG         = "agent_log"         # agent-authored memory artefacts
    CUSTOM            = "custom"            # escape hatch


class AVResult(StrEnum):
    CLEAN    = "clean"
    INFECTED = "infected"
    ERROR    = "error"
    SKIPPED  = "skipped"


class DocStatus(StrEnum):
    """documents.status — strictly forward-moving with `failed` as a sink."""
    RECEIVED     = "received"
    QUARANTINED  = "quarantined"
    REJECTED     = "rejected"
    EXTRACTING   = "extracting"
    NORMALIZED   = "normalized"
    SUMMARIZING  = "summarizing"
    CHUNKING     = "chunking"
    EMBEDDING    = "embedding"
    INDEXED      = "indexed"
    FAILED       = "failed"
