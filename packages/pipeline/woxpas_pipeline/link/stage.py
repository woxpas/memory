"""LinkingStage — Phase 2.2.

Subscribes to doc.extracted_facts. Runs 3 sequential phases (no LLM):

  1. Predicate normalization (Phase 2.2.1 — implemented)
  2. Entity disambiguation   (Phase 2.2.2 — implemented)
  3. Profile fact accretion  (Phase 2.2.3 — implemented)

All three phases are live; the doc.linked event payload carries the real
entity_merged + profile_facts_added counts.

Embedder + canonical predicates are lazy-loaded once per process. The
predicate index lives in memory; it's small (88 system-global rows + any
per-tenant additions) and rebuild on settings change is cheap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import (
    EntityProfileFactRepo,
    EntityRepo,
    OutboxRepo,
    PipelineRunRepo,
    PredicateRepo,
)

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed, publish_doc_linked

log = structlog.get_logger(__name__)


# Cosine threshold for predicate normalization. BGE-M3 puts a raw verb
# phrase ("is leading") next to a canonical predicate's "canonical + aliases"
# embedding ("leads | guides | spearheads") in the 0.55-0.85 band when the
# match is real, and 0.30-0.50 for unrelated phrases. 0.55 catches the
# obvious cases without false positives. Tunable later if smoke tests reveal
# accuracy issues.
_PREDICATE_MATCH_THRESHOLD = 0.55

# Cosine threshold for entity-merge. Names alone produce a different cosine
# distribution than full sentences; "Ada" vs "Ada Lovelace" sits ~0.75-0.85,
# "Ada" vs "Bob" sits ~0.40-0.55. 0.80 is precision-favored — we'd rather
# leave duplicates split (recoverable in v0.2) than collapse distinct people.
# Kept as a separate constant from predicate threshold because the underlying
# distribution is genuinely different.
_ENTITY_MERGE_THRESHOLD = 0.80


@dataclass(slots=True, frozen=True)
class _CanonicalPredicate:
    predicate_id: UUID
    canonical:    str
    kind:         str
    text_for_embedding: str            # "<canonical> | <alias1> | <alias2> | ..."
    identity_relevant: bool


# Maps a predicate kind to a default fact_category for entity_profile_facts.
# Used by phase 2.2.3 accretion. Keyed on `kind`; identity-relevance is the
# eligibility filter (already enforced before we reach this map).
#
# Categories align with the seeds documented in identity.py:
#   bio | profession | comm_style | projects | preferences |
#   expertise | relationships | goals | health | other
_KIND_TO_FACT_CATEGORY: dict[str, str] = {
    "attribute":    "bio",            # has_color, has_size, costs, …
    "preference":   "preferences",    # likes, prefers, dislikes, …
    "locational":   "bio",            # headquartered_in, located_in
    "quantitative": "bio",            # consumes, produces, requires
    "relation":     "relationships",  # friend_of, married_to, works_at, …
    "state":        "bio",            # is_deprecated (only durable state)
    "temporal":     "bio",            # born_on, anniversary_of
}


class LinkingStage:
    name = "link"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("summarize"))   # share LLM-ish budget
        # Per-tenant predicate index — tenant_id → (predicates list, dense matrix).
        # Tenant scoping required because per-tenant predicates can extend the
        # system-global vocabulary. Lazy-loaded on first handle for that tenant.
        self._predicate_cache: dict[UUID, tuple[
            list[_CanonicalPredicate], list[list[float]],
        ]] = {}
        self._predicate_lock: dict[UUID, asyncio.Lock] = {}

    def event_type(self) -> EventType:
        return EventType.DOC_EXTRACTED_FACTS

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
            preds_norm, preds_unmatched = await self._phase_predicate_norm(
                tenant_id, doc_id,
            )
            entities_merged   = await self._phase_entity_disambiguation(
                tenant_id, doc_id,
            )
            profile_facts_added = await self._phase_profile_fact_accretion(
                tenant_id, doc_id,
            )
        except Exception as e:
            log.error("link.failed", doc_id=str(doc_id), error=str(e),
                      error_class=type(e).__name__)
            await self._record_failure(
                tenant_id, doc_id, run_id,
                error_class=type(e).__name__, error_message=str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # Persist run success + emit doc.linked in one transaction.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_repo = PipelineRunRepo(conn)
            outbox   = OutboxRepo(conn)
            await run_repo.succeed(run_id, metrics={
                "predicates_normalized": preds_norm,
                "predicates_unmatched":  preds_unmatched,
                "entities_merged":       entities_merged,
                "profile_facts_added":   profile_facts_added,
            })
            await publish_doc_linked(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id, pipeline_version=pv,
                predicates_normalized=preds_norm,
                predicates_unmatched=preds_unmatched,
                entities_merged=entities_merged,
                profile_facts_added=profile_facts_added,
                causation_id=event.correlation_id,
            )

        log.info("link.ok",
                 doc_id=str(doc_id),
                 preds_norm=preds_norm, preds_unmatched=preds_unmatched,
                 entities_merged=entities_merged,
                 profile_facts_added=profile_facts_added)

    # ─── Phase 1: predicate normalization (2.2.1 — implemented) ─────────────

    async def _phase_predicate_norm(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> tuple[int, int]:
        """For each 'open' relation in the doc:
          - Embed raw predicate text
          - Cosine vs all canonical predicates this tenant can see
          - If top score >= threshold: set predicate_canonical + flip to 'typed'
          - Else: leave as 'open' (linking is best-effort; unmatched relations
            are still queryable by raw predicate text)
        Returns (normalized_count, unmatched_count).
        """
        index = await self._ensure_predicate_index(tenant_id)
        if not index:
            return 0, 0
        canonicals, ref_dense = index

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            open_rows = await conn.fetch(
                """
                SELECT relation_id, predicate
                  FROM relations
                 WHERE tenant_id = $1 AND doc_id = $2 AND predicate_kind = 'open'
                """,
                tenant_id, doc_id,
            )
        if not open_rows:
            return 0, 0

        # Embed all raw predicates in one batch (fast: ~50ms for ~10 strings).
        embedder = await self._ctx.embedder()
        raw_texts = [(r["relation_id"], r["predicate"]) for r in open_rows]
        batch = await embedder.embed(
            [t[1] for t in raw_texts],
            batch_size=len(raw_texts),
        )
        if not batch.dense or len(batch.dense) != len(raw_texts):
            log.warning("link.predicate_embed_partial",
                        doc_id=str(doc_id), expected=len(raw_texts),
                        got=len(batch.dense) if batch.dense else 0)
            return 0, len(raw_texts)

        normalized = 0
        unmatched  = 0

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            pred_repo = PredicateRepo(conn)
            for (relation_id, raw_pred), query_vec in zip(
                raw_texts, batch.dense, strict=True,
            ):
                best_idx, best_score = _argmax_cosine(query_vec, ref_dense)
                if best_score >= _PREDICATE_MATCH_THRESHOLD:
                    canonical = canonicals[best_idx]
                    try:
                        async with conn.transaction():
                            await pred_repo.set_relation_canonical(
                                tenant_id, relation_id,
                                predicate_canonical=canonical.canonical,
                            )
                        normalized += 1
                        log.debug("link.predicate_normalized",
                                  raw=raw_pred, canonical=canonical.canonical,
                                  kind=canonical.kind, score=round(best_score, 4))
                    except Exception as e:
                        log.warning("link.predicate_persist_failed",
                                    raw=raw_pred, error=str(e))
                        unmatched += 1
                else:
                    unmatched += 1
                    log.debug("link.predicate_unmatched",
                              raw=raw_pred, best_score=round(best_score, 4),
                              best_canonical=canonicals[best_idx].canonical)
        return normalized, unmatched

    async def _ensure_predicate_index(
        self, tenant_id: UUID,
    ) -> tuple[list[_CanonicalPredicate], list[list[float]]] | None:
        """Build (or return cached) predicate embedding index for this tenant."""
        cached = self._predicate_cache.get(tenant_id)
        if cached is not None:
            return cached
        lock = self._predicate_lock.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            cached = self._predicate_cache.get(tenant_id)
            if cached is not None:
                return cached

            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                rows = await PredicateRepo(conn).list_visible(tenant_id)
            if not rows:
                log.warning("link.no_predicates_seeded", tenant_id=str(tenant_id))
                return None

            canonicals: list[_CanonicalPredicate] = []
            embed_texts: list[str] = []
            for r in rows:
                aliases = list(r.get("aliases") or [])
                # Concatenate canonical + aliases so the embedding captures the
                # semantic neighborhood. Description is omitted — typically too
                # generic to help similarity.
                text_for_embedding = " | ".join([r["canonical"], *aliases])
                canonicals.append(_CanonicalPredicate(
                    predicate_id=r["predicate_id"],
                    canonical=r["canonical"],
                    kind=r["kind"],
                    text_for_embedding=text_for_embedding,
                    identity_relevant=bool(r.get("identity_relevant", False)),
                ))
                embed_texts.append(text_for_embedding)

            embedder = await self._ctx.embedder()
            batch = await embedder.embed(embed_texts, batch_size=len(embed_texts))
            if not batch.dense or len(batch.dense) != len(canonicals):
                raise RuntimeError(
                    f"embedder returned {len(batch.dense) if batch.dense else 0} "
                    f"vectors for {len(canonicals)} predicates"
                )

            self._predicate_cache[tenant_id] = (canonicals, batch.dense)
            log.info("link.predicate_index_loaded",
                     tenant_id=str(tenant_id), count=len(canonicals),
                     dim=len(batch.dense[0]))
            return self._predicate_cache[tenant_id]

    # ─── Phase 2: entity disambiguation (2.2.2 — implemented) ──────────────

    async def _phase_entity_disambiguation(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> int:
        """Soft-merge entities mentioned by this doc into older same-type
        entities when cosine match ≥ _ENTITY_MERGE_THRESHOLD.

        - "Same-type" prevents a person named "Berlin" from merging into a
          place "Berlin".
        - "Older wins" — the canonical entity is the one with the earlier
          first_mentioned_at; merging onto an established entity preserves
          its history. Ties broken by creation timestamp.
        - Soft merge: sets merged_into_entity_id; FKs on relations/ideas/
          tasks stay pointing at the duplicate (provenance preserved). Retrieval
          follows the chain via COALESCE(merged_into_entity_id, entity_id).
        - Re-running linking on the same doc is idempotent — already-merged
          entities are filtered out by candidates_for_doc + candidates_same_type.
        """
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            entity_repo = EntityRepo(conn)
            doc_entities = await entity_repo.candidates_for_doc(tenant_id, doc_id)
        if not doc_entities:
            return 0

        embedder = await self._ctx.embedder()
        merged_count = 0

        for ent in doc_entities:
            ent_id   = ent["entity_id"]
            ent_name = ent["canonical_name"]
            ent_type = ent["type"]
            ent_first = ent.get("first_mentioned_at") or ent.get("created_at")

            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                pool_candidates = await EntityRepo(conn).candidates_same_type(
                    tenant_id, ent_id, ent_type,
                )
            if not pool_candidates:
                continue

            # Embed (this entity name) + (all candidates) in one batch.
            texts = [ent_name] + [c["canonical_name"] for c in pool_candidates]
            batch = await embedder.embed(texts, batch_size=len(texts))
            if not batch.dense or len(batch.dense) != len(texts):
                log.warning("link.entity_embed_partial",
                            entity_id=str(ent_id),
                            expected=len(texts),
                            got=len(batch.dense) if batch.dense else 0)
                continue

            query_vec   = batch.dense[0]
            cand_vecs   = batch.dense[1:]
            best_idx, best_score = _argmax_cosine(query_vec, cand_vecs)
            if best_idx < 0 or best_score < _ENTITY_MERGE_THRESHOLD:
                log.debug("link.entity_no_match",
                          entity=ent_name, type=ent_type,
                          best_score=round(best_score, 4))
                continue

            best = pool_candidates[best_idx]
            best_first = best.get("first_mentioned_at") or best.get("created_at")
            # Older entity wins. If the candidate IS older, merge our entity
            # into it. If our entity is older, the OTHER one will get merged
            # into us when its own doc gets linked — skip here to avoid
            # double-merging from both sides.
            if best_first is None or ent_first is None:
                continue
            if best_first >= ent_first:
                log.debug("link.entity_skip_newer_canonical",
                          entity=ent_name, candidate=best["canonical_name"],
                          score=round(best_score, 4))
                continue

            try:
                async with self._ctx.pool.tenant_session(tenant_id) as conn:
                    async with conn.transaction():
                        await EntityRepo(conn).merge_into(
                            tenant_id,
                            from_entity_id=ent_id,
                            into_entity_id=best["entity_id"],
                        )
                merged_count += 1
                log.info("link.entity_merged",
                         from_entity=ent_name, from_id=str(ent_id),
                         into_entity=best["canonical_name"],
                         into_id=str(best["entity_id"]),
                         score=round(best_score, 4))
            except Exception as e:
                log.warning("link.entity_merge_failed",
                            entity=ent_name, error=str(e))

        return merged_count

    # ─── Phase 3: profile fact accretion (2.2.3 — implemented) ─────────────

    async def _phase_profile_fact_accretion(
        self, tenant_id: UUID, doc_id: UUID,
    ) -> int:
        """Roll self-entity claims into entity_profile_facts when the claim's
        predicate is identity-relevant (per migration 017 flag).

        Identity vs memory:
          - "Ada lives in Berlin"     (lives_in, identity_relevant=true) → fact
          - "Ada prefers async"       (prefers, identity_relevant=true) → fact
          - "Ada talked to Alice"     (said, identity_relevant=false)   → skip
          - "Ada decided X"           (decided, identity_relevant=false)→ skip

        Idempotent: re-running on the same doc bumps existing facts'
        updated_at + max-confidence rather than duplicating. Conflict
        detection (Phase 2.3) handles the case where the SAME (entity,
        fact_kind) has DIFFERENT fact_value in a new claim.

        Returns count of NEW facts added (not bumps).
        """
        # Resolve the tenant's self-entity once.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            self_entity_id = await conn.fetchval(
                "SELECT self_entity_id FROM tenants WHERE tenant_id = $1",
                tenant_id,
            )
        if self_entity_id is None:
            log.debug("link.no_self_entity", tenant_id=str(tenant_id))
            return 0

        # Build a canonical → _CanonicalPredicate lookup from the cached index.
        index = await self._ensure_predicate_index(tenant_id)
        if not index:
            return 0
        canonicals, _ = index
        by_canonical: dict[str, _CanonicalPredicate] = {
            c.canonical: c for c in canonicals
        }

        # Self-entity claims that have been linked to a canonical predicate.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT relation_id, predicate_canonical, object_entity_id,
                       object_value, claim_text, confidence
                  FROM relations
                 WHERE tenant_id = $1
                   AND doc_id = $2
                   AND subject_entity_id = $3
                   AND predicate_kind = 'typed'
                   AND predicate_canonical IS NOT NULL
                """,
                tenant_id, doc_id, self_entity_id,
            )

        if not rows:
            return 0

        added = 0
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            fact_repo = EntityProfileFactRepo(conn)
            for r in rows:
                canonical_pred = by_canonical.get(r["predicate_canonical"])
                if canonical_pred is None or not canonical_pred.identity_relevant:
                    continue
                fact_value = await self._claim_object_to_fact_value(
                    conn=conn,
                    tenant_id=tenant_id,
                    object_entity_id=r["object_entity_id"],
                    object_value=r["object_value"],
                    claim_text=r["claim_text"],
                )
                if fact_value is None:
                    continue
                fact_category = _KIND_TO_FACT_CATEGORY.get(canonical_pred.kind, "other")
                try:
                    async with conn.transaction():
                        _, was_inserted = await fact_repo.upsert_fact(
                            tenant_id,
                            entity_id=self_entity_id,
                            fact_category=fact_category,
                            fact_kind=canonical_pred.canonical,
                            fact_value=fact_value,
                            confidence=float(r["confidence"] or 0.7),
                            extractor="link",
                            extractor_version="0.1.0",
                        )
                    if was_inserted:
                        added += 1
                        log.info("link.profile_fact_added",
                                 entity_id=str(self_entity_id),
                                 fact_kind=canonical_pred.canonical,
                                 fact_category=fact_category,
                                 value=fact_value)
                    else:
                        log.debug("link.profile_fact_bumped",
                                  fact_kind=canonical_pred.canonical)
                except Exception as e:
                    log.warning("link.profile_fact_failed",
                                fact_kind=canonical_pred.canonical, error=str(e))
        return added

    async def _claim_object_to_fact_value(
        self,
        *,
        conn,
        tenant_id: UUID,
        object_entity_id: UUID | None,
        object_value:     dict[str, Any] | None,
        claim_text:       str | None,
    ) -> dict[str, Any] | None:
        """Build the {fact_value} jsonb for a profile fact from a relation row.

        - If the relation points to another entity (object_entity_id), embed
          the entity's canonical_name + entity_id so the fact is queryable
          both as a string and as an FK.
        - If the relation has a literal value (object_value.text), use that.
        - If neither (shouldn't happen post-extraction), bail.
        """
        if object_entity_id is not None:
            row = await conn.fetchrow(
                "SELECT canonical_name, type FROM entities "
                "WHERE tenant_id = $1 AND entity_id = $2",
                tenant_id, object_entity_id,
            )
            if row is None:
                return None
            return {
                "text":         row["canonical_name"],
                "entity_id":    str(object_entity_id),
                "entity_type":  row["type"],
            }
        if object_value is not None:
            text = object_value.get("text") if isinstance(object_value, dict) else str(object_value)
            if not text:
                return None
            return {"text": str(text)}
        return None

    # ─── Failure path ──────────────────────────────────────────────────────

    async def _record_failure(
        self, tenant_id: UUID, doc_id: UUID, run_id: UUID,
        *,
        error_class: str, error_message: str, retriable: bool,
        causation_id: UUID | None,
    ) -> None:
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_repo = PipelineRunRepo(conn)
            outbox   = OutboxRepo(conn)
            await run_repo.fail(run_id, error_class, error_message, retriable=retriable)
            await publish_doc_failed(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                stage=self.name,
                error_class=error_class, error_message=error_message,
                retriable=retriable,
                causation_id=causation_id,
            )


def _argmax_cosine(query: list[float], refs: list[list[float]]) -> tuple[int, float]:
    """Return (index_of_best_match, best_cosine). BGE-M3 vectors are unit-norm
    so cosine == dot product, but we don't assume that for safety."""
    if not query or not refs:
        return -1, 0.0
    best_idx, best_score = -1, -1.0
    qnorm = sum(x * x for x in query) ** 0.5
    if qnorm == 0.0:
        return -1, 0.0
    for i, ref in enumerate(refs):
        if len(ref) != len(query):
            continue
        dot = sum(x * y for x, y in zip(query, ref, strict=True))
        rnorm = sum(y * y for y in ref) ** 0.5
        if rnorm == 0.0:
            continue
        score = dot / (qnorm * rnorm)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx, best_score
