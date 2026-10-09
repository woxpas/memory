"""Retrieval request/response/filter types — the public contract for the
recall API.

Design principles for cloud-portability (pillar 4):
  - No server-side state in requests beyond what auth provides (tenant_id
    lives outside the request body, plumbed via auth).
  - Cursors are opaque strings (server encodes whatever it needs; client
    treats them as black boxes). Stable iteration across paginated calls.
  - Versioned response shape via `protocol_version` field so future cloud
    deploys can negotiate compatibility.
  - Stateless: every call is fully self-contained. No session affinity needed.
  - Forward-compatible: extra fields ignored on parse (model_config extra=ignore).

Three result shapes covering the full query surface:
  - LIST    — top-K ordered items (default for type/semantic/timeline queries)
  - CARD    — entity profile with counts + recent items (skeleton or full)
  - DETAIL  — single doc/entity full payload (for file lookups)
  - GRAPH   — entity-relation traversal (recursive CTE; depth-limited)
  - STREAM  — timeline-ordered items (alias for LIST with chronological order)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "1"


# ─── Enums ──────────────────────────────────────────────────────────────────

class Intent(StrEnum):
    """The route the engine takes. `auto` = unknown; classifier decides.
    Otherwise the agent's hint takes priority."""
    AUTO          = "auto"
    SEMANTIC      = "semantic"        # hybrid vector + DB enrichment
    TIMELINE      = "timeline"        # time-windowed query (last X / since Y)
    TYPE_LISTING  = "type_listing"    # ideas | tasks | files | scopes
    FILE_LOOKUP   = "file_lookup"     # single doc by id or filename
    ENTITY_CARD   = "entity_card"     # entity skeleton/full
    GRAPH         = "graph"           # entity-relation traversal


class ResultShape(StrEnum):
    LIST    = "list"      # default — top-K ordered ResultItem array
    CARD    = "card"      # EntityCard
    DETAIL  = "detail"    # DocumentDetail / similar single-payload shape
    GRAPH   = "graph"     # GraphView (nodes + edges)
    STREAM  = "stream"    # LIST but explicitly chronological


class SensitivityFilter(StrEnum):
    ANY                = "any"                  # default
    EXCLUDE_SENSITIVE  = "exclude_sensitive"    # for cloud-bound contexts
    ONLY_SENSITIVE     = "only_sensitive"       # ops/audit


class CardMode(StrEnum):
    SKELETON = "skeleton"   # counts + summaries
    FULL     = "full"       # paginated actual data


# ─── Filters ────────────────────────────────────────────────────────────────

class TimeFilter(BaseModel):
    """Time window. Either explicit (since/until) or relative ("last_2_days").
    Engine resolves relative → since/until before querying so SQL is uniform."""
    model_config = ConfigDict(extra="ignore")

    since:    datetime | None = None
    until:    datetime | None = None
    relative: str | None = None
    """Recognized: 'last_hour', 'last_day', 'last_2_days', 'last_week',
    'last_month', 'yesterday', 'today', 'this_week', 'this_month'.
    Engine maps to (since, until) at query time so all clients see the same
    semantics regardless of locale."""


class Filters(BaseModel):
    """Composable filters applied across all backends. Each is optional;
    a backend uses only the filters relevant to it."""
    model_config = ConfigDict(extra="ignore")

    scope_ids:           list[UUID] = Field(default_factory=list)
    scope_slugs:         list[str]  = Field(default_factory=list)
    time:                TimeFilter | None = None
    sensitivity:         SensitivityFilter = SensitivityFilter.ANY
    processing_profiles: list[str]  = Field(default_factory=list)
    entity_ids:          list[UUID] = Field(default_factory=list)
    predicate_kinds:     list[str]  = Field(default_factory=list)
    """For graph traversal: only follow relations whose predicate.kind is in
    this list. Avoids noise from 'mentioned'/'cites' predicates."""

    include_archived:    bool = False
    """Default False — only live (archived_at IS NULL) claims/relations.
    True for history-mode queries."""


# ─── Request ────────────────────────────────────────────────────────────────

class RetrievalRequest(BaseModel):
    """Single-entry recall request. Cloud-portable: no server state needed.

    The engine derives tenant_id from auth — never from this body — so the
    same request can be replayed across instances safely.
    """
    model_config = ConfigDict(extra="ignore")

    request_id:   UUID = Field(default_factory=uuid4)
    """Client-supplied or server-generated. Used for idempotency + tracing."""

    query_text:   str = ""
    """Natural-language query OR structured ID for *_lookup intents.
    File lookup: 'doc:<uuid>' or '"some filename"' (quoted).
    Entity card: 'entity:<uuid>' or '"Ada Lovelace"'.
    Other intents: free-text."""

    intent_hint:  Intent = Intent.AUTO
    """Agent's intent declaration. AUTO falls through to the rule classifier
    + LLM fallback. Anything else is taken at face value (the agent knows)."""

    filters:      Filters = Field(default_factory=Filters)
    shape:        ResultShape | None = None
    """None = engine picks the natural shape for the resolved intent."""

    card_mode:    CardMode = CardMode.SKELETON
    """For ENTITY_CARD intent: skeleton (counts) vs full (paged data)."""

    as_of:        datetime | None = None
    """Bitemporal: query the state of memory as of this timestamp.
    None = now() — current view of memory."""

    limit:        int = 20
    """Soft cap on result items. Engine may return fewer; truncation flag
    set on the response if more matched than fit."""

    cursor:       str | None = None
    """Opaque pagination cursor from a prior response. Server encodes
    whatever state it needs; client treats as a black box."""

    graph_depth:  int = 2
    """For GRAPH intent: max hops from the seed entity. Capped server-side
    to a hard maximum to bound query cost."""

    rerank:       bool = True
    """For SEMANTIC intent: run cross-encoder reranker after hybrid vector
    search. Disable to save latency when ordering quality doesn't matter
    (e.g., quick-glance previews)."""

    enrich_with_entities: bool = True
    """Phase 4.9 — when True (default), the engine detects named entities
    in the query, pulls their rich state (profile facts, active relations,
    active tasks/ideas, open conflicts), traces back to source chunks, and
    merges those into the result items. Also produces an entity_dossier
    text block on the response that synthesis injects as situational frame.

    Set False when the agent wants pure semantic retrieval without the
    "knows you" enrichment (e.g., topic exploration where entity context
    would bias the answer)."""


# ─── Response items ─────────────────────────────────────────────────────────

class ResolutionProvenance(BaseModel):
    """Attached to claims/facts that have been the subject of a conflict
    resolution. Enables agent communications like 'Ada lives in Paris (was
    Berlin until last March — see resolution note)'."""
    model_config = ConfigDict(extra="ignore")

    conflict_id:    UUID
    kind:           Literal["pick", "coexistence", "progression", "dismissed"]
    note:           str | None = None
    resolved_state: str
    """One of conflicts.resolved_state: open|auto_resolved|human_resolved|living|dismissed."""
    resolved_at:    datetime | None = None
    discriminator:  str | None = None
    """For coexistence — the context tag for THIS claim ('home', 'work',
    etc.) so the agent can disambiguate."""


class Provenance(BaseModel):
    """Source citation chain for a result item. Every claim/chunk surfaced
    by the engine carries enough provenance for the agent to quote sources
    or re-fetch full content."""
    model_config = ConfigDict(extra="ignore")

    doc_ids:       list[UUID] = Field(default_factory=list)
    chunk_ids:     list[UUID] = Field(default_factory=list)
    span_ids:      list[UUID] = Field(default_factory=list)
    relation_ids:  list[UUID] = Field(default_factory=list)
    fact_ids:      list[UUID] = Field(default_factory=list)
    open_conflicts: list[UUID] = Field(default_factory=list)
    """Open (unresolved) conflict IDs touching these refs."""
    resolutions:   list[ResolutionProvenance] = Field(default_factory=list)
    """Resolved-conflict context. Empty when nothing to surface."""


class ResultItem(BaseModel):
    """Unified item shape for LIST / STREAM results. `kind` + `metadata`
    keep the structure flexible without proliferating top-level types."""
    model_config = ConfigDict(extra="ignore")

    kind:    Literal["chunk", "doc", "idea", "task", "claim", "fact", "entity", "scope"]
    id:      UUID
    score:   float | None = None
    """Relevance score for ranked queries; None for chronological/listing."""

    title:   str | None = None
    summary: str | None = None
    snippet: str | None = None
    """For chunk results: the source text excerpt."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Kind-specific bag: {processing_profile, status, due_at, ...}."""

    provenance:        Provenance | None = None
    sensitivity_class: str = "standard"
    """Always present; agent uses to gate cloud-bound contexts."""

    created_at:        datetime | None = None
    last_accessed_at:  datetime | None = None


# ─── Card / Detail / Graph result payloads ──────────────────────────────────

class EntityCard(BaseModel):
    """ENTITY_CARD result. skeleton mode: counts + recent. full mode: same
    but with paginated `recent_items` list and `profile_facts` list."""
    model_config = ConfigDict(extra="ignore")

    entity_id:        UUID
    canonical_name:   str
    type:             str
    mention_count:    int = 0
    first_mentioned_at: datetime | None = None
    last_mentioned_at:  datetime | None = None

    counts: dict[str, int] = Field(default_factory=dict)
    """{relations: N, ideas: N, tasks: N, mentions: N, profile_facts: N,
        open_conflicts: N}"""

    profile_facts: list[dict[str, Any]] = Field(default_factory=list)
    """For full mode: all entity_profile_facts rows (paginated)."""

    recent_relations: list[ResultItem] = Field(default_factory=list)
    recent_ideas:     list[ResultItem] = Field(default_factory=list)
    recent_tasks:     list[ResultItem] = Field(default_factory=list)
    recent_mentions:  list[ResultItem] = Field(default_factory=list)


class DocumentDetail(BaseModel):
    """DETAIL result for FILE_LOOKUP. Same as the inspect-doc CLI shape but
    structured for agent consumption."""
    model_config = ConfigDict(extra="ignore")

    doc_id:                UUID
    title:                 str | None = None
    filename:              str | None = None
    processing_profile:    str
    sensitivity_class:     str
    summary:               str | None = None
    topics:                list[str] = Field(default_factory=list)

    chunks_preview:        list[ResultItem] = Field(default_factory=list)
    """Up to 5 chunks for an at-a-glance view. Use cursor pagination for full."""

    canonical_uri:         str | None = None
    raw_uri:               str | None = None
    """Object-store URIs. Reading them requires the tenant DEK; these are
    references, not the content."""


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entity_id:      UUID
    canonical_name: str
    type:           str
    score:          float | None = None
    mention_count:  int = 0


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject_entity_id: UUID
    object_entity_id:  UUID
    predicate_canonical: str | None = None
    predicate_kind:     str | None = None
    claim_text:         str | None = None
    confidence:         float | None = None
    relation_id:        UUID


class GraphPath(BaseModel):
    """A single path through the graph — sequence of edges A→...→B.

    Used for `path_between(a, b)` queries ("how is Ada connected to Bob")
    AND optionally for `traverse(seed)` (each non-root node gets its
    BFS-shortest path from the root in `node_paths`).

    `total_confidence` aggregates the per-edge `confidence` via min — the
    weakest link in the chain dominates, which matches "chain-of-trust"
    semantics. Min is more conservative than product but easier to reason
    about; switch later if telemetry suggests otherwise.
    """
    model_config = ConfigDict(extra="ignore")

    edge_ids:         list[UUID]      # relation_ids in order, length = path length
    node_ids:         list[UUID]      # entity_ids visited in order, length = edges + 1
    length:           int
    total_confidence: float | None = None


class GraphView(BaseModel):
    """GRAPH result. Returned by both `traverse(seed)` and `path_between(a, b)`."""
    model_config = ConfigDict(extra="ignore")
    root_entity_id: UUID
    depth:          int
    nodes:          list[GraphNode] = Field(default_factory=list)
    edges:          list[GraphEdge] = Field(default_factory=list)

    paths:          list[GraphPath] = Field(default_factory=list)
    """For `path_between(a, b)`: shortest paths from a to b. For `traverse`:
    optionally populated with the BFS-shortest path from root to each node
    when `with_paths=True`. Empty otherwise."""

    truncated_at_level: dict[int, int] = Field(default_factory=dict)
    """Reports per-level neighbor truncation: `{level: dropped_count}`.
    Agent surfaces "+N more connections at level X" hints when populated.
    Pillar 4 — never silently lose results without telling the user."""

    target_entity_id: UUID | None = None
    """For `path_between(a, b)`: the destination entity. None for traverse."""

    found:           bool = True
    """`path_between` sets False when no path exists within depth — caller
    distinguishes "no connection" from "didn't run yet"."""


# ─── Hint (truncation contract) ─────────────────────────────────────────────

class Hint(BaseModel):
    """Returned when the engine truncated results below the soft limit OR
    when there's a follow-up action the agent should consider.

    Example for the user's brief:
      message:        "Retrieved 3 of 7 chunks; for full content use file_lookup."
      primary_doc_ids: ['<uuid>', '<uuid>']     # highest-confidence sources
      suggested_action: "file_lookup"
    """
    model_config = ConfigDict(extra="ignore")

    message:          str
    primary_doc_ids:  list[UUID] = Field(default_factory=list)
    suggested_action: str | None = None
    """One of: 'file_lookup', 'entity_card', 'graph', 'narrow_query',
    'paginate'. Agent maps to a follow-up call."""


# ─── Response ───────────────────────────────────────────────────────────────

class RetrievalResult(BaseModel):
    """Full response envelope. `intent_used` reports what the engine
    actually decided so the agent learns from the routing."""
    model_config = ConfigDict(extra="ignore")

    protocol_version: str = PROTOCOL_VERSION
    request_id:       UUID

    intent_used:        Intent
    intent_uncertain:   bool = False
    """True when rules classifier picked but confidence was middling."""
    shape:              ResultShape

    # Exactly one of these is populated based on shape.
    items:    list[ResultItem] = Field(default_factory=list)   # LIST / STREAM
    card:     EntityCard | None = None                          # CARD
    detail:   DocumentDetail | None = None                      # DETAIL
    graph:    GraphView | None = None                           # GRAPH

    total_estimated: int | None = None
    """Best-effort total match count. None when expensive to compute
    (e.g., reranked semantic search)."""

    truncated:    bool = False
    next_cursor:  str | None = None
    hint:         Hint | None = None

    metrics:      dict[str, Any] = Field(default_factory=dict)
    """Latency breakdown (intent_classify_ms, vector_search_ms, rerank_ms,
    db_enrich_ms, total_ms), source counts, etc. Agent + ops can both use."""

    entity_dossier: str | None = None
    """Phase 4.9 — situational frame text built when enrich_with_entities=True
    and the query contains resolvable entity references. Synthesis injects
    this as INTERPRETATION context (not cited; LLM uses it to disambiguate +
    frame the answer; cites only the source chunks for actual claims).

    Example shape:
      'Sarah Chen — Senior engineer; leads alpha project (active).
                   Currently: 3 overdue tasks; alpha deadline 2026-06-01.'

    None when no entities resolved or enrich_with_entities=False."""
