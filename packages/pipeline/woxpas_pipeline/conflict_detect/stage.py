"""ConflictDetectionStage — Phase 2.3.

Subscribes to doc.linked. Three subroutines run sequentially, all SQL-only
(no LLM, no embeddings — fast). Idempotent via dedup_key.

  1. _detect_knowledge_mismatch   — relations (predicate_kind='typed') from
                                     this doc that contradict relations from
                                     other docs on (subject, predicate_canonical).
                                     Excludes preference predicates (handled below).
  2. _detect_preference_mismatch  — same as above filtered to predicate.kind=
                                     preference. severity='low' since opinions
                                     evolve.
  3. _detect_profile_fact_mismatch — entity_profile_facts created/updated by
                                      THIS doc's link run that contradict
                                      existing live facts on (entity, fact_kind).

Architecture decisions:
  - Each detected conflict is created with status='open' — Phase 2.3 does not
    auto-resolve. Resolution is a deliberate action (user/agent/sweep).
  - Same-object claims do NOT create conflicts (those are corroborations,
    handled by relations.confidence updates upstream — currently no-op,
    that's a Phase 3 surfacing-layer concern).
  - Conflict severity:
      knowledge_mismatch    → medium
      preference_mismatch   → low
      profile_fact_mismatch → high (identity contradictions are user-facing)
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import (
    ConflictRepo,
    OutboxRepo,
    PipelineRunRepo,
)

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed

log = structlog.get_logger(__name__)


_PREFERENCE_KINDS = frozenset({"preference"})


class ConflictDetectionStage:
    name = "conflict_detect"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("summarize"))

    def event_type(self) -> EventType:
        return EventType.DOC_LINKED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id: UUID = event.tenant_id
        doc_id:    UUID = event.aggregate_id
        pv             = self._ctx.pipeline.pipeline_version

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_id = await PipelineRunRepo(conn).start(
                tenant_id, doc_id, self.name, pv,
            )

        try:
            knowledge_count   = await self._detect_knowledge_mismatch(tenant_id, doc_id)
            preference_count  = await self._detect_preference_mismatch(tenant_id, doc_id)
            profile_count     = await self._detect_profile_fact_mismatch(tenant_id, doc_id)
        except Exception as e:
            log.error("conflict_detect.failed",
                      doc_id=str(doc_id), error=str(e),
                      error_class=type(e).__name__)
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                await PipelineRunRepo(conn).fail(
                    run_id, type(e).__name__, str(e), retriable=True,
                )
                await publish_doc_failed(
                    OutboxRepo(conn),
                    tenant_id=tenant_id, doc_id=doc_id, pipeline_version=pv,
                    stage=self.name,
                    error_class=type(e).__name__, error_message=str(e),
                    retriable=True, causation_id=event.correlation_id,
                )
            return

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            await PipelineRunRepo(conn).succeed(run_id, metrics={
                "knowledge_mismatch":   knowledge_count,
                "preference_mismatch":  preference_count,
                "profile_fact_mismatch": profile_count,
            })

        log.info("conflict_detect.ok",
                 doc_id=str(doc_id),
                 knowledge=knowledge_count,
                 preference=preference_count,
                 profile=profile_count)

    # ─── Subroutine 1: knowledge mismatch (relations) ─────────────────────

    async def _detect_knowledge_mismatch(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> int:
        """Find relations from THIS doc that contradict relations from OTHER
        docs on the same (subject_entity_id, predicate_canonical).

        Comparison rules:
          - object_entity_id pair: different non-null IDs → conflict
          - object_value pair: different jsonb → conflict
          - mixed (one entity, one literal) → conflict (model said the same
            thing differently and may be wrong)
          - same object → corroboration, no conflict

        Excludes preference predicates (handled by _detect_preference_mismatch).
        Both sides must be live (archived_at IS NULL) to count.
        """
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            pairs = await conn.fetch(
                """
                WITH this_doc AS (
                  SELECT r.relation_id, r.subject_entity_id, r.predicate_canonical,
                         r.object_entity_id, r.object_value, r.doc_id
                    FROM relations r
                   WHERE r.tenant_id  = $1
                     AND r.doc_id     = $2
                     AND r.predicate_kind = 'typed'
                     AND r.predicate_canonical IS NOT NULL
                     AND r.archived_at IS NULL
                ),
                pred_kinds AS (
                  SELECT canonical, kind
                    FROM predicates
                   WHERE tenant_id IS NULL OR tenant_id = $1
                )
                SELECT t.relation_id  AS a_id,
                       t.subject_entity_id,
                       t.predicate_canonical,
                       o.relation_id  AS b_id,
                       t.doc_id       AS a_doc,
                       o.doc_id       AS b_doc,
                       t.object_entity_id  AS a_obj_id,
                       t.object_value      AS a_obj_val,
                       o.object_entity_id  AS b_obj_id,
                       o.object_value      AS b_obj_val
                  FROM this_doc t
                  JOIN relations o
                    ON o.tenant_id           = $1
                   AND o.subject_entity_id   = t.subject_entity_id
                   AND o.predicate_canonical = t.predicate_canonical
                   AND o.predicate_kind      = 'typed'
                   AND o.relation_id        <> t.relation_id
                   AND o.doc_id             <> t.doc_id
                   AND o.archived_at IS NULL
                  JOIN pred_kinds pk ON pk.canonical = t.predicate_canonical
                 WHERE pk.kind NOT IN ('preference')
                   -- Only flag if the objects actually disagree.
                   AND (
                     (t.object_entity_id IS NOT NULL
                       AND o.object_entity_id IS NOT NULL
                       AND t.object_entity_id <> o.object_entity_id)
                     OR (t.object_value IS NOT NULL
                       AND o.object_value IS NOT NULL
                       AND t.object_value <> o.object_value)
                     OR (t.object_entity_id IS NULL AND o.object_entity_id IS NOT NULL)
                     OR (t.object_entity_id IS NOT NULL AND o.object_entity_id IS NULL)
                   )
                """,
                tenant_id, doc_id,
            )

        return await self._record_relation_conflicts(
            tenant_id,
            pairs=pairs,
            conflict_type="knowledge_mismatch",
            severity="medium",
        )

    # ─── Subroutine 2: preference mismatch (relations, kind=preference) ────

    async def _detect_preference_mismatch(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> int:
        """Same query as knowledge mismatch but filtered to preference-kind
        predicates. Lower severity — opinions evolve, not always a real
        contradiction. resolved_state default is 'open' (not 'living') so the
        agent surfaces it; user can dismiss or move to 'living' (coexistence)."""
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            pairs = await conn.fetch(
                """
                WITH this_doc AS (
                  SELECT r.relation_id, r.subject_entity_id, r.predicate_canonical,
                         r.object_entity_id, r.object_value, r.doc_id
                    FROM relations r
                   WHERE r.tenant_id  = $1
                     AND r.doc_id     = $2
                     AND r.predicate_kind = 'typed'
                     AND r.predicate_canonical IS NOT NULL
                     AND r.archived_at IS NULL
                ),
                pred_kinds AS (
                  SELECT canonical, kind
                    FROM predicates
                   WHERE tenant_id IS NULL OR tenant_id = $1
                )
                SELECT t.relation_id AS a_id, t.subject_entity_id,
                       t.predicate_canonical,
                       o.relation_id AS b_id,
                       t.doc_id AS a_doc, o.doc_id AS b_doc,
                       t.object_entity_id AS a_obj_id, t.object_value AS a_obj_val,
                       o.object_entity_id AS b_obj_id, o.object_value AS b_obj_val
                  FROM this_doc t
                  JOIN relations o
                    ON o.tenant_id           = $1
                   AND o.subject_entity_id   = t.subject_entity_id
                   AND o.predicate_canonical = t.predicate_canonical
                   AND o.predicate_kind      = 'typed'
                   AND o.relation_id        <> t.relation_id
                   AND o.doc_id             <> t.doc_id
                   AND o.archived_at IS NULL
                  JOIN pred_kinds pk ON pk.canonical = t.predicate_canonical
                 WHERE pk.kind = 'preference'
                   AND (
                     (t.object_entity_id IS NOT NULL
                       AND o.object_entity_id IS NOT NULL
                       AND t.object_entity_id <> o.object_entity_id)
                     OR (t.object_value IS NOT NULL
                       AND o.object_value IS NOT NULL
                       AND t.object_value <> o.object_value)
                     OR (t.object_entity_id IS NULL AND o.object_entity_id IS NOT NULL)
                     OR (t.object_entity_id IS NOT NULL AND o.object_entity_id IS NULL)
                   )
                """,
                tenant_id, doc_id,
            )

        return await self._record_relation_conflicts(
            tenant_id,
            pairs=pairs,
            conflict_type="preference_mismatch",
            severity="low",
        )

    # ─── Subroutine 3: profile fact mismatch ───────────────────────────────

    async def _detect_profile_fact_mismatch(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> int:
        """Find entity_profile_facts written by THIS doc's link run that
        contradict an existing live fact on (entity_id, fact_kind).

        We can't filter to 'this doc's facts' via doc_id since
        entity_profile_facts has no doc column — fall back to
        link-stage run window via pipeline_runs (same pattern as
        EntityRepo.candidates_for_doc).
        """
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            pairs = await conn.fetch(
                """
                WITH link_run AS (
                  SELECT started_at,
                         COALESCE(finished_at, now()) AS ended_at
                    FROM pipeline_runs
                   WHERE tenant_id = $1 AND doc_id = $2 AND stage = 'link'
                   ORDER BY started_at DESC LIMIT 1
                ),
                fresh_facts AS (
                  SELECT f.fact_id, f.entity_id, f.fact_kind, f.fact_value
                    FROM entity_profile_facts f, link_run lr
                   WHERE f.tenant_id = $1
                     AND f.superseded_by IS NULL
                     AND f.updated_at >= lr.started_at
                     AND f.updated_at <= lr.ended_at
                )
                SELECT  t.fact_id      AS a_id,
                        t.entity_id    AS subject_entity_id,
                        t.fact_kind    AS predicate,
                        o.fact_id      AS b_id,
                        t.fact_value   AS a_obj_val,
                        o.fact_value   AS b_obj_val
                  FROM fresh_facts t
                  JOIN entity_profile_facts o
                    ON o.tenant_id   = $1
                   AND o.entity_id   = t.entity_id
                   AND o.fact_kind   = t.fact_kind
                   AND o.fact_id    <> t.fact_id
                   AND o.superseded_by IS NULL
                 WHERE t.fact_value <> o.fact_value
                """,
                tenant_id, doc_id,
            )

        if not pairs:
            return 0

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            repo = ConflictRepo(conn)
            count = 0
            for p in pairs:
                a_id = p["a_id"]
                b_id = p["b_id"]
                # Order-independent: only the lexically-smaller pair member
                # is the "primary" so dedup key collapses (a,b) and (b,a).
                mention_refs = [
                    {"fact_id": str(a_id), "value": p["a_obj_val"]},
                    {"fact_id": str(b_id), "value": p["b_obj_val"]},
                ]
                _, was_inserted = await repo.find_or_create(
                    tenant_id,
                    conflict_type="profile_fact_mismatch",
                    severity="high",
                    subject_entity_ids=[p["subject_entity_id"]],
                    predicate=p["predicate"],
                    mention_refs=mention_refs,
                    doc_refs=[],
                    detector=self.name,
                    dedup_ids=[str(a_id), str(b_id)],
                )
                if was_inserted:
                    count += 1
            return count

    # ─── Shared: record relation conflicts ─────────────────────────────────

    async def _record_relation_conflicts(
        self, tenant_id: UUID,
        *,
        pairs: list,                          # type: ignore[type-arg]
        conflict_type: str,
        severity: str,
    ) -> int:
        if not pairs:
            return 0
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            repo = ConflictRepo(conn)
            count = 0
            for p in pairs:
                a_id = p["a_id"]
                b_id = p["b_id"]
                mention_refs = [
                    {
                        "relation_id": str(a_id),
                        "doc_id":      str(p["a_doc"]),
                        "object_entity_id": str(p["a_obj_id"]) if p["a_obj_id"] else None,
                        "object_value":     p["a_obj_val"],
                    },
                    {
                        "relation_id": str(b_id),
                        "doc_id":      str(p["b_doc"]),
                        "object_entity_id": str(p["b_obj_id"]) if p["b_obj_id"] else None,
                        "object_value":     p["b_obj_val"],
                    },
                ]
                doc_refs: list[UUID] = []
                if p["a_doc"]:
                    doc_refs.append(p["a_doc"])
                if p["b_doc"] and p["b_doc"] != p["a_doc"]:
                    doc_refs.append(p["b_doc"])

                _, was_inserted = await repo.find_or_create(
                    tenant_id,
                    conflict_type=conflict_type,
                    severity=severity,
                    subject_entity_ids=[p["subject_entity_id"]],
                    predicate=p["predicate_canonical"],
                    mention_refs=mention_refs,
                    doc_refs=doc_refs,
                    detector=self.name,
                    dedup_ids=[str(a_id), str(b_id)],
                )
                if was_inserted:
                    count += 1
            return count
