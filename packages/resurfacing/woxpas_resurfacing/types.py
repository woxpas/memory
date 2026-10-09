"""Resurfacing response types — wire-protocol contracts for the
plug-and-play API any agent (NOYU + 3rd-party MCP) can consume.

The Thinking Brain endpoint (`GET /resurface/context`) returns the
ThinkingBrainContext snapshot — what's "live in the user's working
memory right now". Agents call this each turn (or once per session)
to interpret queries with situational awareness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ─── Sub-types ──────────────────────────────────────────────────────────────

class ActiveScope(BaseModel):
    """A scope (collection / project / topic boundary) recently touched.

    `weight` blends recency + activity volume; agents use it to decide
    which scope context is most relevant for the current query."""
    model_config = ConfigDict(extra="ignore")

    scope_id:        UUID
    slug:            str
    name:            str
    description:     str | None = None
    doc_count_24h:   int = 0
    doc_count_7d:    int = 0
    last_touched_at: datetime | None = None


class ActiveEntity(BaseModel):
    """An entity recently mentioned. mention_count_24h + slope reveals
    whether it's a fresh topic or a long-running one. decay_score
    indicates how much attention it's getting; higher = more dormant."""
    model_config = ConfigDict(extra="ignore")

    entity_id:           UUID
    name:                str
    type:                str
    mention_count:       int = 0
    mentions_24h:        int = 0
    mentions_7d:         int = 0
    last_mentioned_at:   datetime | None = None
    access_count:        int = 0
    """How many times retrieval has surfaced this entity (USER-INTEREST signal).
    Distinct from mention_count which counts ingest-time appearances."""
    last_accessed_at:    datetime | None = None
    decay_score:         float = 1.0
    surface_score:       float = 0.0
    """Composite priority signal (Phase 5a.2.1). Blends mention recency,
    access recency, velocity, centrality, totals, plus open-conflict
    attention boost. Higher = more relevant to surface. 0.0 means
    score-maintainer hasn't run for this entity yet (cold tenant);
    ordering falls back to mention_count + recency."""
    slope_24h_vs_baseline: float | None = None


class TimelineEntry(BaseModel):
    """One event from the timeline. `payload` carries kind-specific data
    (e.g., for 'doc.summarized': summary text + topic list)."""
    model_config = ConfigDict(extra="ignore")

    event_id:    int
    kind:        str
    occurred_at: datetime
    actor:       str | None = None
    doc_id:      UUID | None = None
    entity_id:   UUID | None = None
    idea_id:     UUID | None = None
    task_id:     UUID | None = None
    scope_id:    UUID | None = None
    conflict_id: UUID | None = None
    payload:     dict[str, Any] = Field(default_factory=dict)


class TaskBrief(BaseModel):
    """Compact task summary for the open-threads view."""
    model_config = ConfigDict(extra="ignore")

    task_id:     UUID
    title:       str
    kind:        str
    status:      str
    due_at:      datetime | None = None
    is_overdue:  bool = False
    priority:    str | None = None


class IdeaBrief(BaseModel):
    """Compact idea summary."""
    model_config = ConfigDict(extra="ignore")

    idea_id:           UUID
    title:             str | None = None
    summary:           str
    status:            str
    category:          str | None = None
    last_revisited_at: datetime | None = None


class ConflictBrief(BaseModel):
    """Compact open-conflict summary (calls for user attention)."""
    model_config = ConfigDict(extra="ignore")

    conflict_id: UUID
    kind:        str
    severity:    str
    notes:       str | None = None
    detected_at: datetime
    subject_entity_ids: list[UUID] = Field(default_factory=list)


class OpenThreads(BaseModel):
    """Bundled active-state overview — what the user has on their plate.

    Agents use these to decide what to surface ("you have 3 overdue tasks"),
    when to nudge ("the alpha project conflict has been open for 2 weeks"),
    or to interpret ambiguous queries ("'what's stuck?' → look at overdue
    + blocked tasks)."""
    model_config = ConfigDict(extra="ignore")

    tasks_due_soon:   list[TaskBrief]    = Field(default_factory=list)  # next 7 days
    tasks_overdue:    list[TaskBrief]    = Field(default_factory=list)
    ideas_developing: list[IdeaBrief]    = Field(default_factory=list)
    conflicts_open:   list[ConflictBrief] = Field(default_factory=list)


class SessionSignals(BaseModel):
    """Per-tenant short-term signals.

    `last_n_queries` and `recent_tools_called` populate from
    resurface_observations (Phase 5a.4); empty in v0.1 of the endpoint
    (5a.1) until observations exist. `recent_docs_accessed` works today
    via documents.last_accessed_at."""
    model_config = ConfigDict(extra="ignore")

    last_n_queries:        list[str]   = Field(default_factory=list)
    recent_docs_accessed:  list[UUID]  = Field(default_factory=list)
    recent_tools_called:   list[str]   = Field(default_factory=list)


# ─── Top-level response ─────────────────────────────────────────────────────

class ThinkingBrainContext(BaseModel):
    """The always-on situational snapshot. Agent reads each turn (or once
    per session with delta updates) to interpret the user's query in
    light of what's live in their memory.

    Default agent flow:
      1. user query arrives
      2. agent calls GET /resurface/context (cached for ~60s per session)
      3. agent passes context to its LLM as background frame
      4. agent calls /ask or /recall with the user's question
      5. memory layer enriches further per Phase 4.9 (entity-aware)
      6. agent composes response with both layers of context
    """
    model_config = ConfigDict(extra="ignore")

    protocol_version: str = "1"

    # Time signals
    now:          datetime
    day_of_week:  str            # 'Monday' / 'Tuesday' / etc.
    time_of_day:  str            # 'morning' / 'afternoon' / 'evening' / 'night'
    timezone:     str            # IANA name; falls back to 'UTC' when tenant.timezone unset

    # Optional location signal — null until external feed wires up
    location:     str | None = None

    # Active state
    active_scopes:   list[ActiveScope]   = Field(default_factory=list)
    active_entities: list[ActiveEntity]  = Field(default_factory=list)
    recent_timeline: list[TimelineEntry] = Field(default_factory=list)

    open_threads:    OpenThreads     = Field(default_factory=OpenThreads)
    session_signals: SessionSignals  = Field(default_factory=SessionSignals)

    # Truncation flags — agent knows when there's more to explore
    entities_truncated: bool = False
    scopes_truncated:   bool = False

    # Latency breakdown — agent + ops both find useful
    metrics: dict[str, Any] = Field(default_factory=dict)


# ─── Phase 5a.2 — Dormant Brain (patterns) ──────────────────────────────────

class PatternSignal(BaseModel):
    """One scored pattern signal from resurface_patterns_cache.

    `subject_label` is resolved from the underlying view (entity name,
    idea title, etc) — saves the agent a follow-up lookup. May be None
    if the underlying subject was deleted between scoring and the read.
    """
    model_config = ConfigDict(extra="ignore")

    pattern_id:    UUID
    kind:          str               # 'trending'|'fading'|'forgotten'|'recurring'|'attention'
    subject_kind:  str               # 'entity'|'idea'|'task'|'topic'|'scope'
    subject_id:    UUID
    subject_label: str | None = None
    score:         float
    reason:        str               # human-readable explanation
    computed_at:   datetime
    ttl_at:        datetime          # when this signal goes stale


class PatternsResult(BaseModel):
    """Response shape for GET /resurface/patterns."""
    model_config = ConfigDict(extra="ignore")

    patterns:           list[PatternSignal] = Field(default_factory=list)
    kinds_requested:    list[str]           = Field(default_factory=list)
    total_returned:     int = 0
    cold_start_compute: bool = False
    """True when the request triggered a synchronous score_all() because
    the cache was empty. Subsequent requests hit the warm cache; agents
    can use this signal to back off from polling for a few seconds."""
    metrics:            dict[str, Any] = Field(default_factory=dict)


# ─── Phase 5a.4 — Tool-use pattern engine (observations + preferences) ─────

class ObservationRequest(BaseModel):
    """Body for POST /resurface/observation. Any agent (NOYU + 3rd-party
    MCP) calls this on every interaction so resurfacing builds a
    cross-agent picture of what the user does + what works."""
    model_config = ConfigDict(extra="ignore")

    agent_id:    str = Field(min_length=1, max_length=80)
    """Free-form agent identifier (e.g. 'noyu', 'claude-mcp', 'cursor').
    v0.2: tenant-issued tokens for verified registration."""
    tool_called: str = Field(min_length=1, max_length=120)
    """Which tool was invoked: 'memory.ask', 'memory.recall', etc."""
    skill_id:    str | None = Field(default=None, max_length=80)
    """Optional. The skill this call is serving — Claude Skills, NOYU
    bundled workflows, GPTs, etc. Null for direct tool calls. Enables
    cross-agent skill-aware patterns (e.g., 'writing skill in NOYU is
    more engaged-with than in Claude'). Phase 5a.4.1."""
    tool_input:  dict[str, Any] | None = None
    """The tool's input — for /ask this is the query_text + flags.

    Convention (recommended, not enforced): include `purpose: str` to
    tag what the user is using this call FOR — e.g.
        {"query_text": "...", "purpose": "email_draft"}
        {"query_text": "...", "purpose": "meeting_prep"}
        {"query_text": "...", "purpose": "research_planning"}
    PatternDetector aggregations on `purpose` ship in v0.2 (depends on
    a normalization layer that maps agent-specific labels into canonical
    purpose categories). Storing the field today keeps the data
    forward-compatible.

    NOT encrypted at rest in v0.1 (RLS + 90-day reaper); v0.2 adds
    envelope encryption matching the documents.canonical pattern."""
    outcome:     dict[str, Any] = Field(default_factory=dict)
    """{success: bool, latency_ms: int, user_engaged: bool|null,
        finish_reason: str|null, ...}"""
    context:     dict[str, Any] = Field(default_factory=dict)
    """{time_of_day, day_of_week, scope, recent_query_kind, ...}"""


class UserPreferences(BaseModel):
    """Returned by GET /resurface/preferences. Cross-agent — any agent
    reads this to shape its OWN behavior. Resurfacing computes the
    preferences from observations; the agent decides how to apply them."""
    model_config = ConfigDict(extra="ignore")

    answer_length_preference:    str  = "normal"   # concise | normal | exhaustive
    citation_density_preference: str  = "normal"   # low | normal | high
    surfacing_aggressiveness:    str  = "normal"   # conservative | normal | aggressive
    persona_tone:                dict[str, Any] = Field(default_factory=dict)
    tool_use_preferences:        dict[str, Any] = Field(default_factory=dict)
    """Per-agent preferences, e.g. {'noyu': {short_answers: True}}."""

    # Phase 5a.4 — derived signals from observations
    most_active_hour:            int | None = None  # 0-23 in user TZ; peak observation hour
    most_active_day_of_week:     int | None = None
    """Phase 5a.3.1 — Peak weekday by observation count (0=Mon, 6=Sun,
    ISO convention). None when fewer than 10 observations exist.
    Foundation for adaptive scheduling — agents derive weekly cadence
    from this; memory has NO schedule of its own."""
    top_tools_used:              list[str]   = Field(default_factory=list)
    """Top tools ranked by observation frequency in last 30 days."""
    top_skills_used:             list[str]   = Field(default_factory=list)
    """Phase 5a.4.1 — top skills ranked by observation frequency in
    last 30 days. Empty when no observations carry skill_id (e.g.,
    when only direct tool calls have been made, or when no skill-aware
    agent is wired up yet)."""
    tool_by_hour:                dict[str, list[str]] = Field(default_factory=dict)
    """Phase 5a.3.1 — Per-hour top tools. Keys are stringified hours
    ('0'-'23'), values are top-N tool names. Hours with <3 observations
    are dropped. Empty until observations accumulate."""
    skill_by_hour:               dict[str, list[str]] = Field(default_factory=dict)
    """Phase 5a.3.1 — Per-hour top skills. Same shape as tool_by_hour."""

    updated_at:                  datetime | None = None
    observation_count_30d:       int = 0
    """How many observations informed these preferences. Low counts
    (< 20) → preferences are still cold; agent should weight defaults
    more heavily."""


class ToolSuggestion(BaseModel):
    """One suggested tool for the current context (5a.4 ranks by
    frequency; 5a.4+ adds context-aware sequencing)."""
    model_config = ConfigDict(extra="ignore")

    tool_name:    str
    score:        float
    reason:       str
    last_used_at: datetime | None = None
    invocation_count: int = 0


class ToolSuggestions(BaseModel):
    """Response for GET /resurface/tools?context=..."""
    model_config = ConfigDict(extra="ignore")

    suggestions:   list[ToolSuggestion] = Field(default_factory=list)
    context_used:  dict[str, Any] = Field(default_factory=dict)
    cold_start:    bool = False
    """True when no observations exist yet — suggestions are empty/default."""
    metrics:       dict[str, Any] = Field(default_factory=dict)


# ─── Phase 5a.3.1 — Predicted/Prepared actions ──────────────────────────────

class PredictedActionItem(BaseModel):
    """A memory-internal opportunity surfaced by the dreaming worker.
    Memory generates these autonomously over the user's CAPTURED
    knowledge graph — overdue tasks, open conflicts, dormant ideas,
    stale entities, unscoped docs.

    Agents read these and decide whether to compose a downstream
    `prepared_action` for user review, surface as a digest item,
    or ignore."""
    model_config = ConfigDict(extra="ignore")

    artifact_id:       UUID
    action_kind:       str
    """Taxonomy: task_overdue | conflict_open | idea_dormant |
    entity_stale | doc_unscoped (extensible — agents should treat
    unknown values as 'unhandled' rather than reject)."""
    subject_kind:      str
    """task | conflict | idea | entity | document"""
    subject_id:        UUID
    subject_label:     str | None = None
    urgency:           str = "medium"
    """Phase 5a.3.4 Track 2b — high | medium | low. Agent uses this to
    decide presentation: high = surface in next response; medium =
    include in periodic digest; low = wait for user-initiated review.
    Currently set by doc_unscoped detector based on captured_by + age;
    other detectors default to 'medium'."""
    context:           dict[str, Any] = Field(default_factory=dict)
    """Why this opportunity exists (e.g., {days_overdue: 5, severity:
    'high'}). Shape varies by action_kind."""
    suggested_actions: list[dict[str, Any]] = Field(default_factory=list)
    """List of {name, reversibility, requires_approval} entries.
    Memory does not enforce approval — these are HINTS to the agent
    about which transitions are safe vs destructive."""
    status:            str = "drafted"
    requires_approval: bool = True
    generated_at:      datetime
    expires_at:        datetime | None = None


class PredictedActionListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items:        list[PredictedActionItem] = Field(default_factory=list)
    total:        int = 0
    metrics:      dict[str, Any] = Field(default_factory=dict)


class PredictedQuestionItem(BaseModel):
    """A pre-cached synthesis answer for a likely user query.

    The query template + top-priority subject are encoded in
    `question_text`; the cached `answer_text` is what the agent can
    serve immediately when the user asks something matching the
    template. May be empty until 5a.3.1's QuestionPreCacher runs."""
    model_config = ConfigDict(extra="ignore")

    artifact_id:    UUID
    subject_kind:   str
    """entity | scope | idea | topic"""
    subject_id:     UUID | None = None
    subject_label:  str | None = None
    question_text:  str
    answer_text:    str
    citation_count: int = 0
    cloud_egress:   bool = False
    """True if a cloud LLM was used to synthesize (Phase 4.x cloud-policy
    gate). Agents may want to indicate provenance to the user."""
    generated_at:   datetime
    expires_at:     datetime | None = None


class PredictedQuestionListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items:   list[PredictedQuestionItem] = Field(default_factory=list)
    total:   int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)


class PreparedActionRequest(BaseModel):
    """POST /resurface/prepared-action — agent-composed action posted
    back to memory for cross-agent persistence and approval state.

    Memory does NOT validate the draft itself. The agent is responsible
    for providing context that lets ANY OTHER agent later list, present,
    and (on approval) execute the action via its own tooling."""
    model_config = ConfigDict(extra="ignore")

    agent_id:    str = Field(min_length=1, max_length=80)
    """Which agent is posting (e.g. 'noyu', 'claude-mcp')."""
    action_kind: str = Field(min_length=1, max_length=80)
    """Taxonomy is open — agents agree on shared kinds informally
    (e.g. 'email_response_draft', 'meeting_prep_brief')."""
    subject_label: str | None = None
    """Human-readable name for the action subject (the email's sender,
    the meeting's title, etc.). Optional but strongly recommended."""
    subject_id:    UUID | None = None
    """Memory entity/document this action relates to, if any."""
    draft_content: dict[str, Any] = Field(default_factory=dict)
    """The composed draft. Free-form jsonb — typical fields: subject,
    body, recipient, context_used. Agents are responsible for being
    able to round-trip this on approval into their own execution path."""
    requires_approval: bool = True
    """False only for genuinely safe read-only actions (e.g. cache
    refresh hints). Default safer-by-default."""
    expires_in_hours:  int = Field(default=72, ge=1, le=720)
    """Auto-expire after N hours if not approved. Default 72h."""
    related_entity_ids: list[UUID] = Field(default_factory=list)


class PreparedActionItem(BaseModel):
    """A prepared_action artifact in any state."""
    model_config = ConfigDict(extra="ignore")

    artifact_id:       UUID
    agent_id:          str | None = None
    action_kind:       str
    subject_label:     str | None = None
    subject_id:        UUID | None = None
    draft_content:     dict[str, Any] = Field(default_factory=dict)
    status:            str
    """drafted | approved | executed | rejected | expired"""
    requires_approval: bool = True
    generated_at:      datetime
    expires_at:        datetime | None = None
    approved_at:       datetime | None = None
    executed_at:       datetime | None = None
    rejected_at:       datetime | None = None
    rejected_reason:   str | None = None
    related_entity_ids: list[UUID] = Field(default_factory=list)


class PreparedActionListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items:   list[PreparedActionItem] = Field(default_factory=list)
    total:   int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)


class PreparedActionPostResponse(BaseModel):
    """Response for POST /resurface/prepared-action — returns the
    artifact_id so the agent can reference it in subsequent state
    transitions (approve / reject / mark-executed)."""
    model_config = ConfigDict(extra="ignore")

    artifact_id: UUID
    status:      str = "drafted"
    expires_at:  datetime | None = None


class PreparedActionRejectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = Field(default=None, max_length=500)
