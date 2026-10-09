"""QuestionPreCacher — Phase 5a.3.1 predicted-question pre-cache.

For each tenant where `allow_cloud_for_background_jobs=true`, picks the
top-N highest-priority subjects (by surface_score) and pre-synthesizes
answers to a small set of likely-asked query templates. Answers are
stored as `resurface_artifacts(kind='predicted_question')` with TTL.
When the user later asks a matching question, the agent can serve the
cached answer instantly (or use it as warmed retrieval context).

Cloud-policy gates (locked 2026-05-06):
  - tenants.allow_cloud_for_background_jobs MUST be true. Default false
    means no pre-caching ever happens for that tenant. Pillar 4 stance:
    cloud egress for background work is OPT-IN.
  - The synthesis engine ALSO honors per-request data-sensitivity gates;
    if the source chunks for a subject contain personal/sensitive data
    AND the tenant hasn't allowed it for cloud, synthesis raises and we
    skip THAT subject (other subjects continue).
  - v0.2 will add per-tenant token budget caps via `tenants.budget_caps`
    so cloud-enabled tenants can still bound spend (target: $100/month
    Jarvis-swarm ceiling). v0.1 trusts the caps in synthesis itself.

Subject selection (memory-internal — no agent context yet):
  - Top entities by surface_score (Phase 5a.2.1) — the most-relevant
    things the user has been engaged with
  - Top scopes by recent activity (project context)
  - Cap small (3-5 per pass) so a 60s tenant timeout isn't blown by
    cloud round-trips

Query templates per subject:
  - "Tell me about {subject_label}." — entity card / scope summary
  - "What's open with {subject_label}?" — active threads + tasks

Cache key:
  - `subject_signature` is set to f"qcache:{subject_kind}:{subject_id}:{template_key}"
    so reruns refresh rather than duplicate. We REUSE the same partial
    unique index (`ux_predicted_action_signature`) that constrains
    predicted_action — and the index allows BOTH kinds since its WHERE
    only checks `kind = 'predicted_action'`. Wait: that's wrong, the
    index ONLY covers predicted_action. For predicted_question we need
    a different mechanism.

  Decision: predicted_question uses simple "delete + insert" idempotency
  rather than UPSERT — when a precacher pass starts, it deletes prior
  artifacts for the subjects it's about to refresh. Cleaner than another
  partial unique index.

Carries `cloud_egress_used=true` on every artifact (it's the whole point
of this module). Agents reading the cache see this flag and may want to
indicate provenance to the user.

Dependency note: this module uses a CALLABLE injected by the caller for
synthesis, rather than importing SynthesisEngine directly. That keeps
woxpas-resurfacing's dependency surface small — the dreaming-worker CLI
constructs the engine and supplies the closure.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────────

_QUESTION_TTL = timedelta(hours=6)
"""Answer freshness window. Surface scores update every 30 min in the
dreaming pass; 6h means even un-refreshed predictions remain useful for
several passes before going stale."""

_TOP_ENTITIES_PER_PASS = 3
_TOP_SCOPES_PER_PASS   = 2

_DEFAULT_CLOUD_MODEL = "deepseek-chat"
"""Cheap-cloud default. v0.2: per-tenant override + budget-aware model
selection (drop to local on budget exhaustion)."""

_QUERY_TEMPLATES = [
    ("about",    "Tell me about {label}."),
    ("open",     "What's open or in progress with {label}?"),
]


@dataclass(slots=True)
class SynthesizedPrediction:
    """Shape returned by the synthesize_fn callable. Decouples this
    module from woxpas-synthesis types."""
    answer_text:    str
    citation_count: int = 0
    cloud_egress:   bool = True
    extra:          dict[str, Any] = field(default_factory=dict)


SynthesizeFn = Callable[
    [str, UUID, str | None],            # (query_text, tenant_id, model_name)
    Awaitable[SynthesizedPrediction],
]


# ─── Pre-cacher ────────────────────────────────────────────────────────────

class QuestionPreCacher:
    """Pre-caches synthesized answers for likely user queries. Construct
    once per worker; pass `synthesize_fn` from the dreaming-worker CLI."""

    def __init__(
        self, *, pool, synthesize_fn: SynthesizeFn,    # type: ignore[no-untyped-def]
        cloud_model: str = _DEFAULT_CLOUD_MODEL,
        top_entities_per_pass: int = _TOP_ENTITIES_PER_PASS,
        top_scopes_per_pass:   int = _TOP_SCOPES_PER_PASS,
    ) -> None:
        self._pool          = pool
        self._synthesize_fn = synthesize_fn
        self._cloud_model   = cloud_model
        self._top_entities  = top_entities_per_pass
        self._top_scopes    = top_scopes_per_pass

    async def precache_for_tenant(
        self, *, tenant_id: UUID, dreaming_run_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Pre-cache predictions for a single tenant. Returns counts +
        timing for bookkeeping. No-ops (returns counts={}, ms=…) when
        the tenant has not opted into cloud-for-background-jobs."""
        t_start = time.monotonic()

        if not await self._is_cloud_allowed(tenant_id=tenant_id):
            log.info("question_cache.skipped_local_only",
                     tenant_id=str(tenant_id))
            return {
                "skipped":    "cloud_not_allowed",
                "predictions": 0,
                "ms":         round((time.monotonic() - t_start) * 1000, 1),
            }

        subjects = await self._pick_subjects(tenant_id=tenant_id)
        if not subjects:
            return {
                "skipped":     "no_subjects",
                "predictions": 0,
                "ms":          round((time.monotonic() - t_start) * 1000, 1),
            }

        # Idempotency: drop prior predictions for this subject set so
        # we don't pile up duplicates across passes.
        await self._drop_prior(tenant_id=tenant_id, subjects=subjects)

        ok = 0
        skipped = 0
        for subject in subjects:
            for template_key, template in _QUERY_TEMPLATES:
                query = template.format(label=subject["label"])
                try:
                    pred = await self._synthesize_fn(
                        query, tenant_id, self._cloud_model,
                    )
                except Exception as e:
                    # Per-subject failures (cloud-policy denial,
                    # transient backend errors) are logged + skipped.
                    log.warning("question_cache.subject_failed",
                                tenant_id=str(tenant_id),
                                subject_kind=subject["kind"],
                                subject_id=str(subject["id"]),
                                template=template_key,
                                error_class=type(e).__name__,
                                error=str(e)[:200])
                    skipped += 1
                    continue
                await self._store_prediction(
                    tenant_id=tenant_id,
                    dreaming_run_id=dreaming_run_id,
                    subject=subject, template_key=template_key,
                    query=query, prediction=pred,
                )
                ok += 1

        result = {
            "predictions":     ok,
            "skipped":         skipped,
            "subjects_picked": len(subjects),
            "ms":              round((time.monotonic() - t_start) * 1000, 1),
        }
        log.info("question_cache.done",
                 tenant_id=str(tenant_id), counts=result)
        return result

    # ─── Cloud-policy + subject selection ────────────────────────────────

    async def _is_cloud_allowed(self, *, tenant_id: UUID) -> bool:
        """Read the tenants.allow_cloud_for_background_jobs flag.
        Defaults to false (sovereign-first stance)."""
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT allow_cloud_for_background_jobs "
                "  FROM tenants WHERE tenant_id = $1",
                tenant_id,
            ))

    async def _pick_subjects(
        self, *, tenant_id: UUID,
    ) -> list[dict[str, Any]]:
        """Top entities + scopes by surface signals. Reads via the
        view layer so the contract holds across schema evolution."""
        out: list[dict[str, Any]] = []
        async with self._pool.tenant_session(tenant_id) as conn:
            # Phase 5a.3.2 fix C: filter out empty/blank names so we
            # never generate "Tell me about ." (wasted cloud spend).
            entities = await conn.fetch(
                """
                SELECT entity_id, name, surface_score
                  FROM v_entities
                 WHERE name IS NOT NULL
                   AND length(trim(name)) > 0
                   AND COALESCE(last_mentioned_at, last_accessed_at)
                       > now() - interval '30 days'
                 ORDER BY surface_score DESC NULLS LAST,
                          mention_count DESC
                 LIMIT $1
                """,
                self._top_entities,
            )
            for r in entities:
                out.append({
                    "kind":  "entity",
                    "id":    r["entity_id"],
                    "label": r["name"],
                })

            scopes = await conn.fetch(
                """
                SELECT scope_id, name, slug
                  FROM v_scope_activity
                 WHERE doc_count_7d > 0
                   AND length(trim(COALESCE(name, slug, ''))) > 0
                 ORDER BY doc_count_24h DESC, doc_count_7d DESC
                 LIMIT $1
                """,
                self._top_scopes,
            )
            for r in scopes:
                out.append({
                    "kind":  "scope",
                    "id":    r["scope_id"],
                    "label": r["name"] or r["slug"],
                })

        return out

    # ─── Storage ─────────────────────────────────────────────────────────

    async def _drop_prior(
        self, *, tenant_id: UUID, subjects: list[dict[str, Any]],
    ) -> None:
        """Remove existing predicted_question artifacts for the subjects
        we're about to refresh. Bounds growth + ensures freshness."""
        if not subjects:
            return
        subject_signatures = [
            f"qcache:{s['kind']}:{s['id']}:{tk}"
            for s in subjects
            for tk, _ in _QUERY_TEMPLATES
        ]
        async with self._pool.tenant_session(tenant_id) as conn:
            await conn.execute(
                """
                DELETE FROM resurface_artifacts
                 WHERE kind = 'predicted_question'
                   AND subject_signature = ANY($1::text[])
                """,
                subject_signatures,
            )

    async def _store_prediction(
        self, *,
        tenant_id:       UUID,
        dreaming_run_id: UUID | None,
        subject:         dict[str, Any],
        template_key:    str,
        query:           str,
        prediction:      SynthesizedPrediction,
    ) -> None:
        """One INSERT per generated answer. subject_signature ensures
        a follow-up _drop_prior cleans it on next pass."""
        signature = f"qcache:{subject['kind']}:{subject['id']}:{template_key}"
        async with self._pool.tenant_session(tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO resurface_artifacts (
                    tenant_id, kind, content,
                    expires_at, dreaming_run_id, status,
                    requires_approval, subject_signature,
                    cloud_egress_used
                )
                VALUES (
                    $1::uuid,
                    'predicted_question'::resurface_artifact_kind,
                    jsonb_build_object(
                        'subject_kind',   $2::text,
                        'subject_id',     $3::text,
                        'subject_label',  $4::text,
                        'template_key',   $5::text,
                        'question_text',  $6::text,
                        'answer_text',    $7::text,
                        'citation_count', $8::int,
                        'extra',          $9::jsonb
                    ),
                    now() + ($10::interval),
                    $11::uuid,
                    'drafted'::resurface_artifact_status,
                    false,
                    $12::text,
                    $13::boolean
                )
                """,
                tenant_id,
                subject["kind"], str(subject["id"]), subject["label"],
                template_key, query,
                prediction.answer_text,
                prediction.citation_count,
                prediction.extra,
                _QUESTION_TTL,
                dreaming_run_id,
                signature,
                bool(prediction.cloud_egress),
            )
