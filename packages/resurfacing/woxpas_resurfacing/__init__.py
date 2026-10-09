"""woxpas-resurfacing — cognition layer between memory and agent.

Phase 5a — Thinking Brain (5a.1), Dormant Brain (5a.2), Dreaming (5a.3),
Tool-Use Patterns (5a.4), MCP server (5a.5).

Memory is the durable substrate. Resurfacing is the cognitive layer that
sits between memory and any agent (NOYU + 3rd-party MCP). The contract:
resurfacing reads ONLY from memory's view layer (v_*) and never the base
tables, so memory backend evolution doesn't break this layer (Pillar 4).
"""

from woxpas_resurfacing.dreaming import DreamingWorker
from woxpas_resurfacing.engine import ResurfaceEngine
from woxpas_resurfacing.observations import PatternDetector
from woxpas_resurfacing.opportunities import OpportunityDetector
from woxpas_resurfacing.question_cache import (
    QuestionPreCacher,
    SynthesizedPrediction,
    SynthesizeFn,
)
from woxpas_resurfacing.score_maintainer import ScoreMaintainer
from woxpas_resurfacing.scorer import PatternScorer
from woxpas_resurfacing.settings import DreamingSettings
from woxpas_resurfacing.types import (
    ActiveEntity,
    ActiveScope,
    ConflictBrief,
    IdeaBrief,
    ObservationRequest,
    OpenThreads,
    PatternSignal,
    PatternsResult,
    PredictedActionItem,
    PredictedActionListResponse,
    PredictedQuestionItem,
    PredictedQuestionListResponse,
    PreparedActionItem,
    PreparedActionListResponse,
    PreparedActionPostResponse,
    PreparedActionRejectRequest,
    PreparedActionRequest,
    SessionSignals,
    TaskBrief,
    ThinkingBrainContext,
    TimelineEntry,
    ToolSuggestion,
    ToolSuggestions,
    UserPreferences,
)

__all__ = [
    "ActiveEntity",
    "ActiveScope",
    "ConflictBrief",
    "DreamingSettings",
    "DreamingWorker",
    "IdeaBrief",
    "ObservationRequest",
    "OpenThreads",
    "OpportunityDetector",
    "PatternDetector",
    "PatternScorer",
    "PatternSignal",
    "PatternsResult",
    "PredictedActionItem",
    "PredictedActionListResponse",
    "PredictedQuestionItem",
    "PredictedQuestionListResponse",
    "PreparedActionItem",
    "PreparedActionListResponse",
    "PreparedActionPostResponse",
    "PreparedActionRejectRequest",
    "PreparedActionRequest",
    "QuestionPreCacher",
    "ResurfaceEngine",
    "ScoreMaintainer",
    "SessionSignals",
    "SynthesizeFn",
    "SynthesizedPrediction",
    "TaskBrief",
    "ThinkingBrainContext",
    "TimelineEntry",
    "ToolSuggestion",
    "ToolSuggestions",
    "UserPreferences",
]
