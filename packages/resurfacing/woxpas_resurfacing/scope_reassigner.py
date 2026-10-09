"""ScopeReassigner — Phase 5a.3.3 dreamer pass for unscoped docs.

Walks docs that ended up unscoped after ingest (auto-assign threshold not
reached, no agent hint provided) and retries the scope-match with a
LOWER threshold. Three-track scope model rationale:

  Track 1 — standalone capture: unscoped is valid; user knew what they
            were doing OR the agent didn't have context.
  Track 2 — agent-saved memory: agent should have hinted; if it didn't,
            ingest itself does a second-pass at lower threshold (5a.3.4).
  Track 3a (THIS MODULE) — periodic background retry of whatever still
            ended up unscoped. Lower threshold than ingest, narrower
            time window, only touches genuinely-unscoped docs.

  Track 3b (v0.2) — clustering + new-scope proposal when no existing
            scope matches even at the lowered threshold.

Why this matters for Pillar 2 (accuracy): scope is a recall signal.
/recall accepts scope_ids/slugs filter; surface scoring uses scope
context; Thinking Brain returns active_scopes. Leaving docs unscoped
indefinitely degrades retrieval quality on those docs.

Safety properties:
  - Only touches docs with NO existing scope assignment (never
    overwrites user/agent intent)
  - Only touches docs ingested in the last lookback window (avoids
    churning ancient orphans whose context is gone)
  - Audit-logs every reassignment with the score + threshold used
  - Idempotent — running back-to-back produces identical results
  - Non-fatal — per-doc failures don't poison the pass
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────

_REASSIGN_THRESHOLD = 0.55
"""Cosine threshold for the dreamer-pass reassignment. Lower than the
ingest-time default (0.65) — these are docs the ingest pass already
declined; we accept slightly weaker similarity in the second pass.
NEVER auto-assigns above 0.55 confidence; below that, the doc stays
unscoped and waits for Track 3b clustering or manual triage."""

_LOOKBACK_DAYS  = 30
_PER_PASS_CAP   = 100
_TOP_CANDIDATES = 3


class ScopeReassigner:
    """Retries scope auto-assignment for unscoped docs in the dreaming
    worker tenant pass. Tenant-isolated via tenant_session."""

    def __init__(self, *, pool, qdrant) -> None:               # type: ignore[no-untyped-def]
        self._pool   = pool
        self._qdrant = qdrant

    async def reassign_for_tenant(
        self, *, tenant_id: UUID,
    ) -> dict[str, Any]:
        """Walk unscoped docs, retry scope match, assign on success.
        Returns counts + timing for bookkeeping."""
        t_start = time.monotonic()
        candidates = await self._fetch_unscoped(tenant_id=tenant_id)

        if not candidates:
            return {
                "candidates":  0,
                "reassigned":  0,
                "no_match":    0,
                "errors":      0,
                "ms":          round((time.monotonic() - t_start) * 1000, 1),
            }

        reassigned = 0
        no_match = 0
        errors = 0
        for doc in candidates:
            try:
                if await self._try_reassign(
                    tenant_id=tenant_id,
                    doc_id=doc["doc_id"],
                    embedding_ref=doc["summary_embedding_ref"],
                    captured_by=doc["captured_by"],
                ):
                    reassigned += 1
                else:
                    no_match += 1
            except Exception as e:
                errors += 1
                log.warning("scope_reassigner.doc_failed",
                            tenant_id=str(tenant_id),
                            doc_id=str(doc["doc_id"]),
                            error_class=type(e).__name__,
                            error=str(e)[:200])

        result = {
            "candidates":  len(candidates),
            "reassigned":  reassigned,
            "no_match":    no_match,
            "errors":      errors,
            "ms":          round((time.monotonic() - t_start) * 1000, 1),
        }
        log.info("scope_reassigner.done",
                 tenant_id=str(tenant_id), counts=result)
        return result

    # ─── Helpers ───────────────────────────────────────────────────────────

    async def _fetch_unscoped(
        self, *, tenant_id: UUID,
    ) -> list[dict[str, Any]]:
        """Unscoped docs with a summary embedding ref, within the lookback
        window. Uses LEFT JOIN to find docs with NO entry in doc_scopes —
        critical to avoid touching user-assigned docs."""
        async with self._pool.tenant_session(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT d.doc_id,
                       d.summary_embedding_ref,
                       d.captured_by,
                       d.created_at
                  FROM documents d
                  LEFT JOIN doc_scopes ds USING (doc_id)
                 WHERE ds.doc_id IS NULL
                   AND d.summary_embedding_ref IS NOT NULL
                   AND d.archived_at IS NULL
                   AND d.created_at > now() - make_interval(days => $1)
                 ORDER BY d.created_at DESC
                 LIMIT $2
                """,
                _LOOKBACK_DAYS, _PER_PASS_CAP,
            )
        return [dict(r) for r in rows]

    async def _try_reassign(
        self, *, tenant_id: UUID, doc_id: UUID,
        embedding_ref: UUID, captured_by: str | None,
    ) -> bool:
        """Returns True if the doc was reassigned to a scope."""
        # 1. Pull the doc's summary vector from Qdrant
        vec = await self._fetch_summary_vector(embedding_ref)
        if vec is None:
            log.info("scope_reassigner.no_vector",
                     doc_id=str(doc_id), embedding_ref=str(embedding_ref))
            return False

        # 2. Search scope centroids
        try:
            candidates = await self._qdrant.search_scopes_by_vector(
                tenant_id, vec, limit=_TOP_CANDIDATES, prefer="centroid",
            )
        except Exception as e:
            log.warning("scope_reassigner.qdrant_search_failed",
                        doc_id=str(doc_id), error=str(e))
            return False

        if not candidates:
            return False

        # Filter to candidates above threshold + with non-empty scope_id.
        # Then iterate to skip orphaned vectors (Qdrant has scope point
        # but scopes row deleted/archived) — pick the first valid one.
        async with self._pool.tenant_session(tenant_id) as conn:
            chosen = None
            for cand in candidates:
                if cand["score"] < _REASSIGN_THRESHOLD or not cand.get("scope_id"):
                    break        # candidates are descending by score; rest are below threshold
                cand_scope_id = UUID(cand["scope_id"])
                exists = await conn.fetchval(
                    "SELECT 1 FROM scopes WHERE scope_id = $1 AND archived_at IS NULL",
                    cand_scope_id,
                )
                if exists:
                    chosen = (cand_scope_id, cand["score"])
                    break
                log.info("scope_reassigner.scope_orphaned",
                         doc_id=str(doc_id),
                         scope_id=str(cand_scope_id),
                         score=round(cand["score"], 4),
                         hint="qdrant has vector but scopes row missing or archived")
            if chosen is None:
                return False
            scope_id, score = chosen

            from woxpas_storage import DocScopeRepo, ScopeRepo, TimelineRepo
            doc_scope = DocScopeRepo(conn)
            scope_repo = ScopeRepo(conn)
            timeline = TimelineRepo(conn)
            # NOTE: doc_scopes.assigned_by has a CHECK constraint
            # accepting only {user, classifier, agent, adapter, rule}.
            # Reassigner is a classifier path (ML-based scope match),
            # just running later than ingest. The dreamer-vs-ingest
            # distinction is preserved in the timeline_events row
            # (actor='scope_reassigner') below.
            inserted = await doc_scope.assign(
                tenant_id, doc_id, scope_id,
                assigned_by="classifier",
                auto_assigned=True,
                confidence=score,
            )
            if inserted:
                await scope_repo.member_count_delta(tenant_id, scope_id, +1)
                await timeline.record(
                    tenant_id, "doc.summarized",
                    doc_id=doc_id, scope_id=scope_id,
                    actor="scope_reassigner",
                    payload={
                        "stage": "scope_reassigner",
                        "score": score,
                        "threshold": _REASSIGN_THRESHOLD,
                        "captured_by": captured_by,
                    },
                )
                log.info("scope_reassigner.reassigned",
                         tenant_id=str(tenant_id),
                         doc_id=str(doc_id),
                         scope_id=str(scope_id),
                         score=round(score, 4),
                         captured_by=captured_by)
                return True
            else:
                # Race: another process assigned in between. Treat as
                # success-equivalent (the doc is now scoped).
                log.info("scope_reassigner.already_assigned",
                         doc_id=str(doc_id), scope_id=str(scope_id))
                return False

    async def _fetch_summary_vector(
        self, embedding_ref: UUID,
    ) -> list[float] | None:
        """Pull the dense summary vector by point_id. Mirrors the pattern
        in QdrantClient.search_similar_summaries."""
        from woxpas_storage.qdrant.collections import Collection, Vec
        try:
            retrieved = await self._qdrant._client.retrieve(   # type: ignore[attr-defined]
                collection_name=Collection.SUMMARIES.value,
                ids=[str(embedding_ref)],
                with_vectors=True,
            )
        except Exception as e:
            log.warning("scope_reassigner.qdrant_retrieve_failed",
                        embedding_ref=str(embedding_ref), error=str(e))
            return None

        if not retrieved:
            return None
        vec_field = retrieved[0].vector
        if isinstance(vec_field, dict):
            return list(vec_field.get(Vec.DENSE.value) or []) or None
        return list(vec_field) if vec_field is not None else None
