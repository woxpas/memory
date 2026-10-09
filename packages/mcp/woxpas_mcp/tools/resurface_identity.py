"""resurface_build_identity — self-service identity builder.

One MCP call writes everything the user wants to share about themselves
directly to the knowledge graph as user-asserted facts (confidence=1.0).
No extraction pipeline, no LLM guessing, no delay. The agent presents
the questions, the user fills what they want, and this tool persists it.

Designed for cold-start: a new tenant has no identity until documents
accumulate and the extraction pipeline runs. This tool skips that wait.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field

from woxpas_mcp.context import ToolContext
from woxpas_mcp.registry import tool

log = structlog.get_logger(__name__)


class Basics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=200)
    nickname:     str | None = Field(default=None, max_length=100)
    pronouns:     str | None = Field(default=None, max_length=50)


class Work(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role:     str | None = Field(default=None, max_length=200)
    company:  str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=200)


class Communication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tone:                 str | None = Field(default=None, max_length=100)
    answer_length:        str | None = Field(default=None, max_length=50)
    citation_preference:  str | None = Field(default=None, max_length=50)


class BuildIdentityInput(BaseModel):
    """Structured identity fields. All optional — fill what you know, skip
    what you don't. The agent should present these to the user as a form
    and call this tool once with all answers."""
    model_config = ConfigDict(extra="forbid")

    basics:        Basics | None = None
    work:          Work | None = None
    communication: Communication | None = None
    location:      str | None = Field(default=None, max_length=200)
    interests:     list[str] | None = Field(default=None, max_length=20)
    expertise:     list[str] | None = Field(default=None, max_length=20)
    goals:         list[str] | None = Field(default=None, max_length=10)
    bio:           str | None = Field(default=None, max_length=2000)


@tool(
    name="resurface_build_identity",
    description=(
        "Build the user's identity profile from structured Q&A. "
        "ONLY call this if the user explicitly agrees — never auto-trigger. "
        "When the user asks 'what do you know about me?' and the entity "
        "card is sparse, ASK: 'Want to build your identity profile? It "
        "takes a minute and helps me personalize everything.' If they say "
        "yes, present the questions and call this tool once with all "
        "answers.\n\n"
        "All fields are optional — only what the user provides is stored. "
        "Facts are user-asserted (confidence=1.0), written directly to "
        "the knowledge graph. Instant, no pipeline delay."
    ),
    input_model=BuildIdentityInput,
)
async def resurface_build_identity(
    *, ctx: ToolContext, input: BuildIdentityInput,
) -> dict[str, Any]:
    tenant_id = ctx.tenant_id

    # Resolve self-entity.
    async with ctx.pipeline_ctx.pool.tenant_session(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT self_entity_id FROM tenants WHERE tenant_id = $1",
            tenant_id,
        )
    if row is None or row["self_entity_id"] is None:
        return {"stored": 0, "error": "no self-entity found for this tenant"}
    self_id: UUID = row["self_entity_id"]

    facts: list[tuple[str, str, dict[str, Any]]] = []

    def _add(category: str, kind: str, value: Any) -> None:
        facts.append((category, kind, {"text": str(value)}))

    # Basics
    if input.basics:
        b = input.basics
        if b.display_name:
            _add("bio", "display_name", b.display_name)
            async with ctx.pipeline_ctx.pool.tenant_session(tenant_id) as conn:
                await conn.execute(
                    "UPDATE entities SET canonical_name = $1 WHERE entity_id = $2",
                    b.display_name, self_id,
                )
        if b.nickname:
            _add("bio", "nickname", b.nickname)
        if b.pronouns:
            _add("bio", "pronouns", b.pronouns)

    # Work
    if input.work:
        w = input.work
        if w.role:
            _add("profession", "role", w.role)
        if w.company:
            _add("profession", "company", w.company)
        if w.industry:
            _add("profession", "industry", w.industry)

    # Communication
    if input.communication:
        c = input.communication
        if c.tone:
            _add("comm_style", "tone", c.tone)
            # Build the JSON with json.dumps so a tone containing quotes,
            # backslashes, or braces is escaped — never concatenated raw into
            # a JSON literal (that would corrupt the document or inject structure).
            tone_profile = json.dumps(
                {"default": {"general": {"tone": c.tone}}}
            )
            try:
                async with ctx.pipeline_ctx.pool.tenant_session(tenant_id) as conn:
                    await conn.execute(
                        """
                        INSERT INTO persona_tone (tenant_id, profile)
                        VALUES ($1, $2::jsonb)
                        ON CONFLICT (tenant_id) DO UPDATE
                        SET profile = persona_tone.profile || $2::jsonb
                        """,
                        tenant_id,
                        tone_profile,
                    )
            except Exception:
                # A persona-tone write failure must not fail the whole identity
                # build (the facts above are already persisted), but it must be
                # visible — a silent swallow here looked like success.
                log.warning("resurface_identity.persona_tone_write_failed", exc_info=True)
        if c.answer_length:
            _add("comm_style", "answer_length", c.answer_length)
        if c.citation_preference:
            _add("comm_style", "citation_preference", c.citation_preference)

    # Location
    if input.location:
        _add("bio", "location", input.location)

    # Interests
    if input.interests:
        for item in input.interests:
            _add("preferences", "interest", item)

    # Expertise
    if input.expertise:
        for item in input.expertise:
            _add("expertise", "skill", item)

    # Goals
    if input.goals:
        for item in input.goals:
            _add("goals", "goal", item)

    # Bio
    if input.bio:
        _add("bio", "summary", input.bio)

    # Persist all facts in one transaction
    stored = 0
    if facts:
        async with ctx.pipeline_ctx.pool.tenant_session(tenant_id) as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO entity_profile_facts (
                        entity_id, tenant_id, fact_category, fact_kind,
                        fact_value, confidence, is_user_asserted,
                        extractor, extractor_version
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, 1.0, true,
                              'identity_builder', '0.1.0')
                    """,
                    [(self_id, tenant_id, cat, kind, val)
                     for cat, kind, val in facts],
                )
        stored = len(facts)

    return {
        "stored": stored,
        "entity_id": str(self_id),
        "categories": list({f[0] for f in facts}),
        "hint": f"Wrote {stored} identity facts. Use memory_recall "
                f"with intent_hint=entity_card to see the full profile.",
    }
