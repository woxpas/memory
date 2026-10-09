"""Extraction prompt module — profile-aware, single-pass-per-chunk.

Architecture:
  - Stable JSON schema across all 10 profiles (entities, ideas, tasks,
    claims, topics). Profile-specific guidance shifts what to PRIORITIZE,
    not what's legal output.
  - One prompt per chunk (chunks are non-overlapping ≤1024-token segments
    produced by the chunk stage). Full LLM attention per chunk → no silent
    content loss the way a single capped-window pass would have.
  - response_format='json_object' enforced at the backend level (Qwen-3B
    + llama-cpp + DeepSeek + Anthropic all honor this through the
    LlmBackend Protocol).

Predicate kinds align with the predicates registry seeded in migration 013:
  attribute | relation | temporal | quantitative | locational |
  communicative | reference | state | preference | other

The LLM works in NAMES (strings), not UUIDs. The stage resolves entity
names to entity_ids via EntityRepo.get_or_create_by_name. Owner names ("me",
"I") map to the tenant's self_entity_id at persistence time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from woxpas_runtime import (
    Message,
    build_hardened_system_prompt,
    expect_json_object,
    wrap_source,
)

# Bumped on schema/prompt change — triggers targeted re-extraction on docs
# that ran under an older prompt.
#   0.1.0 — initial profile-aware schema
#   0.2.0 — adds prompt-injection hardening (delimiters + schema rules)
EXTRACTION_PROFILE_VERSION = "0.2.0"


# ─── Schema description (stable across profiles) ────────────────────────────

_SCHEMA_DESCRIPTION = """\
Output a single valid JSON object with these top-level keys (all arrays;
empty arrays are valid when nothing applies):

  entities: [
    {"name": "<canonical name>",
     "type": "person|org|place|concept|event|product",
     "context": "<one short phrase, what this entity is in this chunk>"}
  ]

  ideas: [
    {"title": "<short title, 1-6 words>",
     "summary": "<1-2 sentence description>",
     "category": "<insight|hypothesis|observation|decision|question|other>",
     "owner": "<entity name; use \\"me\\" if it's the user's own>"}
  ]

  tasks: [
    {"title": "<imperative title>",
     "description": "<1-2 sentences, optional>",
     "kind": "task|deadline|event|reminder",
     "due_iso": "<YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ if datetime; null if none>",
     "owner": "<entity name; use \\"me\\" by default>"}
  ]

  claims: [
    {"subject": "<entity name>",
     "predicate": "<verb phrase as written, e.g., 'is friends with', 'lives in'>",
     "object": "<entity name OR a literal string value>",
     "claim_text": "<the surface sentence, verbatim or near-verbatim>",
     "kind": "attribute|relation|temporal|quantitative|locational|communicative|reference|state|preference|other"}
  ]

  topics: ["<short noun phrase>", ...]   // 0-5 topics
"""

_SCHEMA_RULES = """\
Rules:
  - Output ONLY the JSON object. No markdown, no commentary, no prose.
  - If the chunk has no extractable content for a category, return [] for it.
  - Names must match the surface form in the chunk (case as written; the
    pipeline normalizes them downstream).
  - For \"me\" / \"I\" / \"my\" → use \"me\" as the owner/subject string. The
    pipeline resolves it to the tenant's self entity.
  - Predicate text should be the verb phrase as the chunk uses it. Don't
    normalize — that's the linking stage's job.
  - claim_text should be a direct quote (or near-quote) from the chunk.
  - Tasks need a real action; don't invent tasks from descriptive prose.
"""


# ─── Profile-specific prioritization ────────────────────────────────────────
#
# Each block tells the model what's MOST relevant for that profile. The
# schema is the same; this just nudges attention. Order: most likely
# common profiles first.

_PROFILE_GUIDANCE: dict[str, str] = {
    "personal": (
        "Profile: personal note (thoughts, journal, voice notes, daily plans).\n"
        "Prioritize: people the user mentions, emotions/feelings as preference\n"
        "claims (kind='preference'), plans/intentions as ideas, mentioned\n"
        "tasks/deadlines, observations about daily life. \"Me/I/my\" refers to\n"
        "the user."
    ),
    "identity_seed": (
        "Profile: identity seed — the user's self-description for the agent.\n"
        "Prioritize: the user's traits, profession, expertise, projects,\n"
        "preferences, communication style, relationships. Encode these as\n"
        "claims with subject=\"me\". Most claims here are kind='attribute' or\n"
        "'preference'. Be exhaustive — every fact about the user matters."
    ),
    "creative_artefact": (
        "Profile: creative work (lyrics, poetry, prose, drafts).\n"
        "Prioritize: themes, motifs, mentioned people/places, key phrases\n"
        "as ideas. Don't extract narrator-as-task; this is creative content.\n"
        "Keep entity extraction conservative — only proper nouns the work\n"
        "actually names."
    ),
    "contract": (
        "Profile: legal/contractual document.\n"
        "Prioritize: parties (entities, type='org' or 'person'), monetary\n"
        "amounts and dates as claims (kind='quantitative'/'temporal'),\n"
        "obligations as tasks (kind='task' or 'deadline'), governing\n"
        "jurisdictions and venues as locational claims, termination/renewal\n"
        "clauses as claims (kind='temporal' or 'state')."
    ),
    "reference": (
        "Profile: reference material (article, paper, manual, book).\n"
        "Prioritize: factual claims (kind='attribute'), authors and citations\n"
        "as entities, key concepts as ideas, mentioned methods/results."
    ),
    "code": (
        "Profile: source code or technical documentation.\n"
        "Prioritize: symbols (functions, classes, modules) as entities\n"
        "(type='concept'), design decisions as ideas (category='decision'),\n"
        "tradeoffs as claims (kind='preference' or 'state'), TODO/FIXME\n"
        "lines as tasks. External dependencies as entities."
    ),
    "correspondence": (
        "Profile: email, message thread, or chat transcript.\n"
        "Prioritize: senders and recipients as entities, requests as tasks,\n"
        "commitments as claims (kind='communicative'), decisions as ideas\n"
        "(category='decision'), follow-up dates as tasks (kind='deadline')."
    ),
    "calendar_event": (
        "Profile: meeting notes or calendar event.\n"
        "Prioritize: attendees as entities, agenda items as ideas, follow-up\n"
        "actions as tasks, decisions as ideas (category='decision'). The\n"
        "event's time/location go as claims (temporal/locational)."
    ),
    "agent_log": (
        "Profile: agent activity log or tool-call transcript.\n"
        "Prioritize: tool calls as ideas (category='decision'), errors as\n"
        "claims (kind='state'), outcomes as claims, follow-ups as tasks.\n"
        "User instructions are claims with subject=\"me\"."
    ),
    "custom": (
        "Profile: custom (no specific structure assumed).\n"
        "Extract whatever fits the schema. Be conservative — when in doubt,\n"
        "leave a category empty rather than over-extracting."
    ),
}


def _profile_block(processing_profile: str) -> str:
    return _PROFILE_GUIDANCE.get(processing_profile, _PROFILE_GUIDANCE["custom"])


# ─── Public API ─────────────────────────────────────────────────────────────

def build_messages(
    chunk_text: str,
    *,
    processing_profile: str,
    doc_title: str | None = None,
    doc_id:    UUID | str | None = None,
    chunk_id:  UUID | str | None = None,
) -> list[Message]:
    """Two-message chat completion: system (schema + profile + injection-rules)
    + user (delimited chunk source).

    Chunk text is wrapped in WOXPAS source delimiters with sanitize=True —
    chunk text is non-overlapping ≤1024-token text from canonical_blocks, so
    a stray delimiter collision is implausible and we'd rather mangle than
    refuse. doc_title is included as an attribute on the source delimiter
    for traceability.
    """
    task = (
        _profile_block(processing_profile)
        + "\n\n"
        + _SCHEMA_DESCRIPTION
        + "\n"
        + _SCHEMA_RULES
    )
    extraction_schema = (
        '{"entities": [...], "ideas": [...], "tasks": [...], '
        '"claims": [...], "topics": [...]}'
    )
    system = build_hardened_system_prompt(
        role="a precise information extractor",
        task=task,
        output_schema=extraction_schema,
        extra_constraints=(
            "GROUNDING: every entity, idea, task, and claim you output MUST be\n"
            "supported by surface text in the source chunk. Do NOT fabricate\n"
            "rows. If the chunk has no extractable content for a category,\n"
            "return [] for it. claim_text MUST be a verbatim or near-verbatim\n"
            "quote from the chunk."
        ),
    )
    user = wrap_source(
        chunk_text,
        doc_id=str(doc_id) if doc_id else None,
        chunk_id=str(chunk_id) if chunk_id else None,
        source_id=f"title={doc_title}" if doc_title else None,
        sanitize=True,
    )
    return [
        Message(role="system", content=system),
        Message(role="user",   content=user),
    ]


@dataclass(slots=True)
class ExtractedEntity:
    name:    str
    type:    str
    context: str = ""


@dataclass(slots=True)
class ExtractedIdea:
    title:    str | None
    summary:  str
    category: str | None
    owner:    str | None


@dataclass(slots=True)
class ExtractedTask:
    title:       str
    description: str | None
    kind:        str
    due_iso:     str | None
    owner:       str | None


@dataclass(slots=True)
class ExtractedClaim:
    subject:    str
    predicate:  str
    object:     str | dict[str, Any]
    claim_text: str
    kind:       str


@dataclass(slots=True)
class ExtractionResult:
    """Per-chunk parsed output. Empty arrays are valid (no extractable content)."""
    entities: list[ExtractedEntity]
    ideas:    list[ExtractedIdea]
    tasks:    list[ExtractedTask]
    claims:   list[ExtractedClaim]
    topics:   list[str]
    raw:      str                     # provider response text (debug/audit)


_VALID_ENTITY_TYPES   = {"person", "org", "place", "concept", "event", "product"}
_VALID_IDEA_CATS      = {"insight", "hypothesis", "observation", "decision",
                         "question", "other"}
_VALID_TASK_KINDS     = {"task", "deadline", "event", "reminder"}
_VALID_PREDICATE_KINDS = {"attribute", "relation", "temporal", "quantitative",
                          "locational", "communicative", "reference", "state",
                          "preference", "other"}


def parse_extraction(raw: str) -> ExtractionResult:
    """Best-effort parse via expect_json_object. Never raises — bad fields
    default to safe values so the pipeline never crashes on flaky LLM output.
    Caller treats this as advisory and persists what's valid."""
    obj: dict[str, Any] = {}
    try:
        obj = expect_json_object(raw)
    except (ValueError, TypeError):
        obj = {}

    return ExtractionResult(
        entities=_parse_entities(obj.get("entities") or []),
        ideas=_parse_ideas(obj.get("ideas") or []),
        tasks=_parse_tasks(obj.get("tasks") or []),
        claims=_parse_claims(obj.get("claims") or []),
        topics=[str(t).strip() for t in (obj.get("topics") or []) if str(t).strip()][:8],
        raw=raw,
    )


def _parse_entities(items: list) -> list[ExtractedEntity]:
    out: list[ExtractedEntity] = []
    for it in items[:64]:    # generous upper bound; LLMs rarely produce more
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        etype = str(it.get("type") or "concept").strip().lower()
        if etype not in _VALID_ENTITY_TYPES:
            etype = "concept"
        out.append(ExtractedEntity(
            name=name, type=etype,
            context=str(it.get("context") or "").strip()[:200],
        ))
    return out


def _parse_ideas(items: list) -> list[ExtractedIdea]:
    out: list[ExtractedIdea] = []
    for it in items[:32]:
        if not isinstance(it, dict):
            continue
        summary = str(it.get("summary") or "").strip()
        if not summary:
            continue
        cat = str(it.get("category") or "").strip().lower() or None
        if cat is not None and cat not in _VALID_IDEA_CATS:
            cat = "other"
        out.append(ExtractedIdea(
            title=str(it.get("title") or "").strip()[:200] or None,
            summary=summary,
            category=cat,
            owner=str(it.get("owner") or "").strip() or None,
        ))
    return out


def _parse_tasks(items: list) -> list[ExtractedTask]:
    out: list[ExtractedTask] = []
    for it in items[:32]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        kind = str(it.get("kind") or "task").strip().lower()
        if kind not in _VALID_TASK_KINDS:
            kind = "task"
        due_iso_raw = it.get("due_iso")
        due_iso = str(due_iso_raw).strip() if due_iso_raw else None
        # Sanity — don't try to parse here; the stage parses with safe fallback.
        out.append(ExtractedTask(
            title=title[:280],
            description=str(it.get("description") or "").strip() or None,
            kind=kind,
            due_iso=due_iso,
            owner=str(it.get("owner") or "").strip() or None,
        ))
    return out


def _parse_claims(items: list) -> list[ExtractedClaim]:
    out: list[ExtractedClaim] = []
    for it in items[:64]:
        if not isinstance(it, dict):
            continue
        subject = str(it.get("subject") or "").strip()
        predicate = str(it.get("predicate") or "").strip()
        if not subject or not predicate:
            continue
        obj_raw = it.get("object")
        if isinstance(obj_raw, dict):
            obj: str | dict[str, Any] = obj_raw
        else:
            obj = str(obj_raw or "").strip()
            if not obj:
                continue
        kind = str(it.get("kind") or "attribute").strip().lower()
        if kind not in _VALID_PREDICATE_KINDS:
            kind = "other"
        out.append(ExtractedClaim(
            subject=subject,
            predicate=predicate[:120],
            object=obj if isinstance(obj, dict) else obj[:500],
            claim_text=str(it.get("claim_text") or "").strip()[:500] or predicate,
            kind=kind,
        ))
    return out
