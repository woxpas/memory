"""woxpas-synthesis — citation-bound answer generation over retrieved memory.

Architecture:
  RetrievalResult → CitationSource[] → prompt.build_messages → LlmBackend
  → streamed prose with `<c id="N"/>` tags → parser.parse → SynthesizedAnswer
  → (Phase 4.4) verifier marks each Citation/Claim → API streams to client

The synthesis package is intentionally model-agnostic. It produces messages
for any LlmBackend (local / OpenAI-compat / Anthropic / reasoning variants).
The streaming and citation-verification pieces in Phase 4.3-4.4 attach
verifier output to each Citation; Phase 4.6 streams everything via SSE.
"""

from woxpas_synthesis.engine import LlmFactory, SynthesisEngine
from woxpas_synthesis.parser import (
    parse,
    render_terminal_with_ansi,
)
from woxpas_synthesis.prompt import build_messages
from woxpas_synthesis.schema import (
    SYNTHESIS_PROFILE_VERSION,
    Citation,
    CitationSource,
    Claim,
    SynthesizedAnswer,
    VerificationStatus,
)
from woxpas_synthesis.types import (
    AnswerEvent,
    ContinuationEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    SourcesEvent,
    SynthesisEvent,
    SynthesisRequest,
    VerifyEvent,
)
from woxpas_synthesis.verifier import CitationVerifier, VerifierConfig

__all__ = [
    "SYNTHESIS_PROFILE_VERSION",
    "AnswerEvent",
    # Schema
    "Citation",
    "CitationSource",
    # Verifier
    "CitationVerifier",
    "Claim",
    "ContinuationEvent",
    "DeltaEvent",
    "DoneEvent",
    "ErrorEvent",
    # Engine
    "LlmFactory",
    "SourcesEvent",
    "SynthesisEngine",
    "SynthesisEvent",
    # Request + events
    "SynthesisRequest",
    "SynthesizedAnswer",
    "VerificationStatus",
    "VerifierConfig",
    "VerifyEvent",
    # Prompt
    "build_messages",
    # Parser + rendering
    "parse",
    "render_terminal_with_ansi",
]
