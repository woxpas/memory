"""Tier 3 refine stage — combines soft heuristics + summary-topic semantics.

Subscribes to doc.summarized. Runs in parallel with chunk/embed (both descend
from doc.normalized). Upgrades documents.sensitivity_class to 'sensitive' when:

  - a soft-tier keyword hit is corroborated by a summary-topic semantic match
    in the same category, OR
  - the summary embedding has a strong cosine match (>= STRONG_TOPIC_THRESHOLD)
    to a sensitive-category reference embedding alone (LLM-produced topics are
    a strong signal — if it produced "medical condition" topics, the doc IS
    medical).

Reference category embeddings are computed once per process at first call and
cached on the stage instance. BGE-M3 ONNX (already in the runtime catalog),
all-local, no LLM, no cloud egress.

This is a defense-in-depth complement to Tier 3a (extract-stage hard heuristics)
and Tier 2 (summarizer LLM judgment). All three tiers OR-combine via the
monotonic upgrade in DocumentRepo.set_sensitivity_sensitive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import DocumentRepo, PipelineRunRepo

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.sensitivity import SoftHit, soft_tier_scan

log = structlog.get_logger(__name__)


# Cosine threshold for "soft hit + topic match in same category" upgrade.
# Calibrated against scope-assign behavior: BGE-M3 cosines for clearly-related
# pairs land 0.55–0.85; 0.55 is permissive enough to catch "medical condition"
# vs "medical record patient" without drowning in false positives.
_CORROBORATED_TOPIC_THRESHOLD = 0.55

# Higher bar for "topic match alone, no soft keyword hit" upgrade. The LLM
# producing topics that strongly cluster with a sensitive category is itself
# the signal — but we want it solid before flipping the flag without other
# corroboration.
_STRONG_TOPIC_THRESHOLD = 0.70


# Reference-category descriptions. Lowercased natural-language strings —
# BGE-M3 handles these directly. Categories MUST align with the soft-tier
# heuristic categories so corroboration logic can pair them.
_CATEGORY_REFERENCE_TEXTS: dict[str, str] = {
    "medical": (
        "medical record patient diagnosis prescription doctor physician "
        "hospital clinic treatment health condition therapy"
    ),
    "financial": (
        "bank account financial statement balance transfer payslip tax "
        "return income mortgage loan investment portfolio salary"
    ),
    "legal_personal": (
        "legal document contract non-disclosure agreement settlement court "
        "order divorce custody will testament estate"
    ),
    "identity_doc": (
        "passport driver licence national identity card social security "
        "number tax id visa green card"
    ),
}


@dataclass(slots=True, frozen=True)
class _CategoryMatch:
    """One reference-category cosine result."""
    category: str
    score:    float


class SensitivityRefineStage:
    name = "sensitivity_refine"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("summarize"))   # share summarize budget
        # Reference embeddings — computed lazily on first handle() call.
        self._ref_dense: dict[str, list[float]] | None = None
        self._ref_lock = asyncio.Lock()

    def event_type(self) -> EventType:
        return EventType.DOC_SUMMARIZED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _ensure_ref_embeddings(self) -> dict[str, list[float]]:
        if self._ref_dense is not None:
            return self._ref_dense
        async with self._ref_lock:
            if self._ref_dense is not None:
                return self._ref_dense
            embedder = await self._ctx.embedder()
            categories = list(_CATEGORY_REFERENCE_TEXTS.keys())
            texts      = [_CATEGORY_REFERENCE_TEXTS[c] for c in categories]
            batch = await embedder.embed(texts, batch_size=len(texts))
            if not batch.dense or len(batch.dense) != len(categories):
                raise RuntimeError(
                    "embedder did not return dense vectors for all categories"
                )
            self._ref_dense = dict(zip(categories, batch.dense, strict=True))
            log.info("sensitivity_refine.refs_loaded",
                     categories=categories, dim=len(batch.dense[0]))
            return self._ref_dense

    async def _handle(self, event: Event) -> None:
        tenant_id: UUID = event.tenant_id
        doc_id:    UUID = event.aggregate_id
        pv             = self._ctx.pipeline.pipeline_version

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc = await DocumentRepo(conn).get(tenant_id, doc_id)
            if doc is None:
                log.warning("sensitivity_refine.doc_missing", doc_id=str(doc_id))
                return
            run_id = await PipelineRunRepo(conn).start(
                tenant_id, doc_id, self.name, pv,
            )

        # Skip if already sensitive — monotonic up-only means we can't
        # downgrade, and re-running heuristics is wasteful. Still record
        # the run as a no-op for observability.
        if doc.get("sensitivity_class") == "sensitive":
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                await PipelineRunRepo(conn).succeed(run_id, metrics={
                    "skipped": "already_sensitive",
                })
            return

        summary = (doc.get("summary") or "").strip()
        topics  = list(doc.get("topics_inferred") or [])
        if not summary and not topics:
            # Nothing to classify — most likely a tiny-doc shortcut on a
            # very short body. Tier 3a in extract handled hard patterns;
            # nothing more to do here.
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                await PipelineRunRepo(conn).succeed(run_id, metrics={
                    "skipped": "no_summary_or_topics",
                })
            return

        # 1. Soft tier on summary + topics joined (cheap, ~1ms).
        soft_text = summary + "\n" + " ".join(topics)
        soft_hits: list[SoftHit] = soft_tier_scan(soft_text)
        soft_categories = {h.category for h in soft_hits}

        # 2. Topic-semantic match — embed (summary + topics) once, compare
        #    to each pre-computed reference category embedding.
        embedder    = await self._ctx.embedder()
        ref_dense   = await self._ensure_ref_embeddings()
        query_text  = summary + " " + " ".join(topics)
        batch       = await embedder.embed([query_text], batch_size=1)
        query_dense = batch.dense[0] if batch.dense else None
        category_matches: list[_CategoryMatch] = []
        if query_dense is not None:
            for category, ref_vec in ref_dense.items():
                score = _cosine(query_dense, ref_vec)
                category_matches.append(_CategoryMatch(category, score))

        # 3. Decide.
        decision_reason: str | None = None
        decision_score:  float      = 0.0

        # Tier 3 corroborated upgrade: soft hit + topic match in same category
        for cm in category_matches:
            if cm.category in soft_categories and cm.score >= _CORROBORATED_TOPIC_THRESHOLD:
                decision_reason = f"refine_corroborated:{cm.category}"
                decision_score  = cm.score
                break

        # Tier 3 strong-topic-only upgrade
        if decision_reason is None:
            for cm in category_matches:
                if cm.score >= _STRONG_TOPIC_THRESHOLD:
                    decision_reason = f"refine_topic:{cm.category}"
                    decision_score  = cm.score
                    break

        # 4. Apply if any.
        if decision_reason is not None:
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                await DocumentRepo(conn).set_sensitivity_sensitive(
                    tenant_id, doc_id, decision_reason,
                )
            log.info("sensitivity_refine.upgraded",
                     doc_id=str(doc_id), reason=decision_reason,
                     score=round(decision_score, 4),
                     soft_hits=[h.token for h in soft_hits[:5]])

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            await PipelineRunRepo(conn).succeed(run_id, metrics={
                "soft_hits":        [h.token for h in soft_hits[:8]],
                "category_scores":  {c.category: round(c.score, 4)
                                     for c in category_matches},
                "decision_reason":  decision_reason,
                "decision_score":   round(decision_score, 4) if decision_reason else None,
                "upgraded":         decision_reason is not None,
            })


def _cosine(a: list[float], b: list[float]) -> float:
    """Plain-Python cosine. Vectors are unit-norm out of BGE-M3 already, so
    cosine == dot product, but we don't assume that for safety."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(dot / (na * nb))
