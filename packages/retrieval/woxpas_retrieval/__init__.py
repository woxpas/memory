"""woxpas-retrieval — query layer over the memory store.

Single entry point: `RetrievalEngine.recall(request) -> RetrievalResult`. The
engine routes by intent (semantic / timeline / type_listing / file_lookup /
entity_card / graph) to the right backend, applies conflict-aware enrichment,
bumps mention counters, and shapes the result for the caller.

Designed cloud-portable from day one — the request/response types are
self-contained, server-side state is minimal (only the auth-derived
tenant_id leaves the boundary; cursors are opaque base64 strings the server
serializes/deserializes).
"""

from woxpas_retrieval.engine import RetrievalEngine
from woxpas_retrieval.enrichment import (
    attach_conflict_provenance,
    build_truncation_hint,
    bump_mentions,
)
from woxpas_retrieval.intent import (
    ClassifyResult,
    ExtractedFilters,
    classify,
    classify_by_rules,
    classify_with_llm,
    extract_filters,
)
from woxpas_retrieval.types import (
    PROTOCOL_VERSION,
    CardMode,
    DocumentDetail,
    EntityCard,
    Filters,
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphView,
    Hint,
    Intent,
    Provenance,
    ResolutionProvenance,
    ResultItem,
    ResultShape,
    RetrievalRequest,
    RetrievalResult,
    SensitivityFilter,
    TimeFilter,
)

__all__ = [
    "PROTOCOL_VERSION",
    "CardMode",
    "ClassifyResult",
    "DocumentDetail",
    "EntityCard",
    "ExtractedFilters",
    "Filters",
    "GraphEdge",
    "GraphNode",
    "GraphPath",
    "GraphView",
    "Hint",
    "Intent",
    "Provenance",
    "ResolutionProvenance",
    "ResultItem",
    "ResultShape",
    "RetrievalEngine",
    "RetrievalRequest",
    "RetrievalResult",
    "SensitivityFilter",
    "TimeFilter",
    "attach_conflict_provenance",
    "build_truncation_hint",
    "bump_mentions",
    "classify",
    "classify_by_rules",
    "classify_with_llm",
    "extract_filters",
]
