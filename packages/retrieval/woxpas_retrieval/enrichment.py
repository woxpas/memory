"""Retrieval enrichment — three concerns wrapping backend results before
they go out to the agent.

  1. Conflict-aware provenance — for any item carrying relation_ids or
     fact_ids, attach `open_conflicts` + `resolutions` so the agent can
     communicate "Ada lives in Lisbon (was Berlin until last month)" rather
     than serving a stale claim with no context. Bridges Phase 2.3
     (conflicts table) into retrieval.

  2. Mention bumping — every entity/idea/task/doc surfaced gets its
     `access_count` incremented + `last_accessed_at` set to now(). Powers
     downstream decay/momentum/surface scoring distinct from ingest-time
     mention_count. v0.2 will switch to bump-on-selection (agent feedback
     API) for stronger signal — current bump-on-retrieval is the v1
     simplification.

  3. Truncation hints — when results were capped (vector-stage budget hit,
     listing exceeded limit), build a Hint with primary file IDs so the
     agent can offer "want full content of <doc>?" as a follow-up.

The orchestrator (Phase 3.8) calls these IN ORDER after backend dispatch:
enrichment → bumping → hint-building → response.

Pillars:
  - Speed: enrichment + bump are batched (one SQL per kind, not per item)
  - Accuracy: pillar-2 wins via conflict provenance — no stale claims served
    without context
  - Security: bump writes are scoped by tenant_id (RLS); only public columns
    touched (no DEK access)
  - Sovereign: works with any storage that exposes pool + ConflictRepo
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from woxpas_retrieval.types import (
    Hint,
    Intent,
    ResolutionProvenance,
    ResultItem,
)

if TYPE_CHECKING:
    from woxpas_storage import PostgresPool

log = structlog.get_logger(__name__)


# ─── 1. Conflict-aware provenance ───────────────────────────────────────────

# Conflicts.resolved_state maps to the public-API ResolutionProvenance.kind
# enum used by the agent. This is the single point of translation between
# storage and retrieval contracts.
_RESOLVED_STATE_TO_KIND = {
    "auto_resolved":   "pick",          # agent / sweep auto-picked a winner
    "human_resolved":  "pick",          # user picked
    "living":          "coexistence",   # both stand, different contexts
    "dismissed":       "dismissed",
    # 'open' — no kind yet; conflict surfaces as `open_conflicts[]` instead.
}


async def attach_conflict_provenance(
    pool: PostgresPool,
    *,
    tenant_id: UUID,
    items: list[ResultItem],
) -> None:
    """For every ResultItem with provenance, look up conflicts and attach
    them at the most precise level available. Mutates `items` in place.

    Three precision tiers (a single conflict surfaces at most ONCE per item
    via dedup-by-conflict_id):

      1. relation_ids / fact_ids — direct (the item IS the claim/fact under conflict)
      2. chunk_ids               — relations EXTRACTED FROM this chunk that conflict
      3. doc_ids                 — relations from anywhere in this doc that conflict
                                   (transitive — a chunk about Ada's birthday in a
                                    doc that ALSO contains a contested preference
                                    surfaces both, since the agent should know the
                                    source doc has open issues)

    All three queries batched. Pillar-2 (accuracy): never serve a chunk
    without telling the agent its source has unresolved contradictions.
    """
    if not items:
        return

    # Collect every reference we care about.
    relation_ids: set[UUID] = set()
    fact_ids:     set[UUID] = set()
    chunk_ids:    set[UUID] = set()
    doc_ids:      set[UUID] = set()
    for it in items:
        if it.provenance is None:
            continue
        relation_ids.update(it.provenance.relation_ids)
        fact_ids.update(it.provenance.fact_ids)
        chunk_ids.update(it.provenance.chunk_ids)
        doc_ids.update(it.provenance.doc_ids)

    if not (relation_ids or fact_ids or chunk_ids or doc_ids):
        return

    # Single batched query covering both indices on conflicts.
    async with pool.tenant_session(tenant_id) as conn:
        conflicts_by_relation: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        conflicts_by_fact:     dict[UUID, list[dict[str, Any]]] = defaultdict(list)

        if relation_ids:
            rows = await conn.fetch(
                """
                SELECT conflict_id, type, severity, resolved_state,
                       resolved_value, notes, predicate, mention_refs,
                       resolved_at
                  FROM conflicts
                 WHERE tenant_id = $1
                   AND mention_refs @> ANY (
                     SELECT jsonb_build_array(jsonb_build_object('relation_id', rid::text))
                       FROM unnest($2::uuid[]) AS rid
                   )
                """,
                tenant_id, list(relation_ids),
            )
            for r in rows:
                rd = dict(r)
                # Each conflict can reference multiple relation_ids.
                for ref in (rd.get("mention_refs") or []):
                    rid_str = ref.get("relation_id") if isinstance(ref, dict) else None
                    if not rid_str:
                        continue
                    try:
                        rid = UUID(rid_str)
                    except (ValueError, TypeError):
                        continue
                    if rid in relation_ids:
                        conflicts_by_relation[rid].append(rd)

        if fact_ids:
            rows = await conn.fetch(
                """
                SELECT conflict_id, type, severity, resolved_state,
                       resolved_value, notes, predicate, mention_refs,
                       resolved_at
                  FROM conflicts
                 WHERE tenant_id = $1
                   AND mention_refs @> ANY (
                     SELECT jsonb_build_array(jsonb_build_object('fact_id', fid::text))
                       FROM unnest($2::uuid[]) AS fid
                   )
                """,
                tenant_id, list(fact_ids),
            )
            for r in rows:
                rd = dict(r)
                for ref in (rd.get("mention_refs") or []):
                    fid_str = ref.get("fact_id") if isinstance(ref, dict) else None
                    if not fid_str:
                        continue
                    try:
                        fid = UUID(fid_str)
                    except (ValueError, TypeError):
                        continue
                    if fid in fact_ids:
                        conflicts_by_fact[fid].append(rd)

        # Tier 2 — chunk_id transitive: conflicts on relations that were
        # extracted from these specific chunks. relations.chunk_id is set
        # by the extraction stage when available.
        conflicts_by_chunk: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        if chunk_ids:
            rows = await conn.fetch(
                """
                SELECT DISTINCT r.chunk_id, c.conflict_id, c.type, c.severity,
                       c.resolved_state, c.resolved_value, c.notes,
                       c.predicate, c.resolved_at
                  FROM conflicts c
                  JOIN LATERAL jsonb_array_elements(c.mention_refs) mr ON true
                  JOIN relations r
                    ON r.tenant_id   = c.tenant_id
                   AND r.relation_id = (mr ->> 'relation_id')::uuid
                   AND r.archived_at IS NULL
                 WHERE c.tenant_id = $1
                   AND r.chunk_id  = ANY ($2::uuid[])
                """,
                tenant_id, list(chunk_ids),
            )
            for r in rows:
                conflicts_by_chunk[r["chunk_id"]].append(dict(r))

        # Tier 3 — doc_id transitive: conflicts on any relation belonging
        # to the source doc. Surfaces "this chunk's source has open issues"
        # even when the chunk itself didn't yield a directly-conflicting
        # relation.
        conflicts_by_doc: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        if doc_ids:
            rows = await conn.fetch(
                """
                SELECT DISTINCT r.doc_id, c.conflict_id, c.type, c.severity,
                       c.resolved_state, c.resolved_value, c.notes,
                       c.predicate, c.resolved_at
                  FROM conflicts c
                  JOIN LATERAL jsonb_array_elements(c.mention_refs) mr ON true
                  JOIN relations r
                    ON r.tenant_id   = c.tenant_id
                   AND r.relation_id = (mr ->> 'relation_id')::uuid
                   AND r.archived_at IS NULL
                 WHERE c.tenant_id = $1
                   AND r.doc_id    = ANY ($2::uuid[])
                """,
                tenant_id, list(doc_ids),
            )
            for r in rows:
                conflicts_by_doc[r["doc_id"]].append(dict(r))

    # Attach to each item, gathering from all 4 precision tiers.
    attached_count = 0
    for it in items:
        if it.provenance is None:
            continue
        item_conflicts: list[dict[str, Any]] = []
        # Tier 1 — direct relation/fact match
        for rid in it.provenance.relation_ids:
            item_conflicts.extend(conflicts_by_relation.get(rid, []))
        for fid in it.provenance.fact_ids:
            item_conflicts.extend(conflicts_by_fact.get(fid, []))
        # Tier 2 — chunk-level transitive (relations extracted from this chunk)
        for cid in it.provenance.chunk_ids:
            item_conflicts.extend(conflicts_by_chunk.get(cid, []))
        # Tier 3 — doc-level transitive (any relation in the source doc)
        for did in it.provenance.doc_ids:
            item_conflicts.extend(conflicts_by_doc.get(did, []))
        if not item_conflicts:
            continue

        # De-dup by conflict_id (an item with multiple relation_ids could
        # match the same conflict twice).
        seen: set[UUID] = set()
        unique = []
        for c in item_conflicts:
            cid = c["conflict_id"]
            if cid in seen:
                continue
            seen.add(cid)
            unique.append(c)

        new_open: list[UUID] = list(it.provenance.open_conflicts)
        new_resolutions: list[ResolutionProvenance] = list(it.provenance.resolutions)

        for c in unique:
            state = c["resolved_state"]
            if state == "open":
                new_open.append(c["conflict_id"])
                continue
            kind = _RESOLVED_STATE_TO_KIND.get(state)
            if kind is None:
                continue
            note = c.get("notes")
            new_resolutions.append(ResolutionProvenance(
                conflict_id    = c["conflict_id"],
                kind           = kind,                note           = note,
                resolved_state = state,
                resolved_at    = c.get("resolved_at"),
            ))

        # Replace the Provenance with an enriched copy (Pydantic models are
        # frozen by Config — replace-by-construction).
        it.provenance = it.provenance.model_copy(update={
            "open_conflicts": new_open,
            "resolutions":    new_resolutions,
        })
        attached_count += 1

    if attached_count > 0:
        log.info("retrieval.enrichment.conflicts_attached",
                 items_with_conflicts=attached_count,
                 unique_conflicts=len({c["conflict_id"]
                                       for cs in conflicts_by_relation.values()
                                       for c in cs}))


# ─── 2. Mention bumping ─────────────────────────────────────────────────────

async def bump_mentions(
    pool: PostgresPool,
    *,
    tenant_id: UUID,
    items: list[ResultItem],
) -> None:
    """Increment access_count + set last_accessed_at on every entity / idea /
    task / doc surfaced. Batched: one UPDATE per kind, not per item.

    Kinds without access tracking (chunk, scope, claim, fact) skip silently —
    chunk-access is implicit in doc-access; claim/fact accesses are folded
    into the entity/relation rows they belong to.
    """
    if not items:
        return

    by_kind: dict[str, set[UUID]] = defaultdict(set)
    for it in items:
        by_kind[it.kind].add(it.id)

    async with pool.tenant_session(tenant_id) as conn:
        if by_kind.get("doc"):
            await conn.execute(
                """
                UPDATE documents
                   SET access_count     = access_count + 1,
                       last_accessed_at = now()
                 WHERE tenant_id = $1 AND doc_id = ANY ($2::uuid[])
                """,
                tenant_id, list(by_kind["doc"]),
            )
        if by_kind.get("entity"):
            await conn.execute(
                """
                UPDATE entities
                   SET access_count     = access_count + 1,
                       last_accessed_at = now()
                 WHERE tenant_id = $1 AND entity_id = ANY ($2::uuid[])
                """,
                tenant_id, list(by_kind["entity"]),
            )
        if by_kind.get("idea"):
            await conn.execute(
                """
                UPDATE ideas
                   SET access_count     = access_count + 1,
                       last_accessed_at = now()
                 WHERE tenant_id = $1 AND idea_id = ANY ($2::uuid[])
                """,
                tenant_id, list(by_kind["idea"]),
            )
        if by_kind.get("task"):
            await conn.execute(
                """
                UPDATE tasks_deadlines
                   SET access_count     = access_count + 1,
                       last_accessed_at = now()
                 WHERE tenant_id = $1 AND task_id = ANY ($2::uuid[])
                """,
                tenant_id, list(by_kind["task"]),
            )

    log.debug("retrieval.enrichment.bumped",
              kind_counts={k: len(v) for k, v in by_kind.items()
                          if k in {"doc", "entity", "idea", "task"}})


# ─── 3. Truncation hints ────────────────────────────────────────────────────

# How many top-scored items are flagged as `primary_doc_ids` in the hint.
# Picked to give the agent a small actionable set, not the full result.
_PRIMARY_DOC_LIMIT = 3


def build_truncation_hint(
    *,
    items:           list[ResultItem],
    truncated:       bool,
    total_estimated: int | None,
    intent:          Intent,
) -> Hint | None:
    """Build a Hint describing what the agent should do next if results
    were truncated. Returns None when no hint is warranted.

    Cases:
      - truncated=True  →  emit hint with primary doc IDs + paginate hint
      - intent_uncertain (not handled here; caller flags on response)
      - empty results   →  None (caller decides if a "no results" message
                                   is itself a hint)
    """
    if not truncated:
        return None
    if not items:
        return None

    # Pick the "primary" docs — top items that are docs OR have a doc_id
    # in their provenance. Order: by score (where present) then by position.
    primary_doc_ids: list[UUID] = []
    seen_docs: set[UUID] = set()
    for it in items:
        if len(primary_doc_ids) >= _PRIMARY_DOC_LIMIT:
            break
        if it.kind == "doc":
            if it.id not in seen_docs:
                seen_docs.add(it.id)
                primary_doc_ids.append(it.id)
        elif it.provenance and it.provenance.doc_ids:
            for did in it.provenance.doc_ids:
                if did in seen_docs:
                    continue
                seen_docs.add(did)
                primary_doc_ids.append(did)
                if len(primary_doc_ids) >= _PRIMARY_DOC_LIMIT:
                    break

    # Compose a human-readable message describing what was returned vs. what
    # exists. For semantic queries we know how many chunks/docs we kept;
    # for type listings we surface "show more by paginating".
    shown = len(items)
    if total_estimated is not None and total_estimated > shown:
        shown_msg = f"Showing {shown} of {total_estimated} matches."
    else:
        shown_msg = f"Showing {shown} matches; more available."

    # Suggest the right follow-up action for the intent.
    suggested_action: str | None
    if intent == Intent.SEMANTIC and primary_doc_ids:
        suggested_action = "file_lookup"
        action_msg = (
            f" For full content of the top match, use file_lookup with "
            f"doc:{primary_doc_ids[0]}."
        )
    elif intent in {Intent.TIMELINE, Intent.TYPE_LISTING, Intent.FILE_LOOKUP}:
        suggested_action = "paginate"
        action_msg = " Use the next_cursor to load more."
    elif intent == Intent.ENTITY_CARD:
        suggested_action = "entity_card"
        action_msg = " Switch to card_mode='full' for paginated detail."
    elif intent == Intent.GRAPH:
        suggested_action = "graph"
        action_msg = (
            " Increase graph_depth or narrow predicate_kinds for a richer "
            "view."
        )
    else:
        suggested_action = "paginate"
        action_msg = ""

    return Hint(
        message=shown_msg + action_msg,
        primary_doc_ids=primary_doc_ids,
        suggested_action=suggested_action,
    )
