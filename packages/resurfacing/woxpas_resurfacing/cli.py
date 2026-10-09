"""woxpas-dreaming-worker entry point.

Standalone process, mirrors woxpas-worker pattern. Constructs a Postgres
pool, instantiates DreamingWorker, runs until SIGINT.

Optional Phase 5a.3.1 — predicted-question pre-cache. When
WOXPAS_DREAMING_ENABLE_QUESTION_CACHE=true, the CLI also constructs the
full synthesis stack (embedder + retrieval + LLM factory + verifier)
and supplies a synthesize closure to QuestionPreCacher. When false (the
default), the worker stays light: it runs cheap detectors only and the
question_cache module is unused.

Optional: can be skipped on minimal-compute installs. Without it,
ScoreMaintainer + PatternScorer still run on demand via the
/resurface/refresh-scores endpoint or the /resurface/patterns
cold-start path; signals just stale gracefully between manual triggers.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from woxpas_storage import PostgresPool, PostgresSettings, QdrantClient, QdrantSettings

from woxpas_resurfacing.dreaming import DreamingWorker
from woxpas_resurfacing.question_cache import (
    QuestionPreCacher,
    SynthesizedPrediction,
)
from woxpas_resurfacing.settings import DreamingSettings

log = structlog.get_logger(__name__)


async def _amain() -> int:
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )

    settings = DreamingSettings()
    if not settings.enabled:
        log.info("dreaming_worker.disabled",
                 reason="WOXPAS_DREAMING_ENABLED=false")
        return 0

    pool = await PostgresPool.from_settings(PostgresSettings())

    # Phase 5a.3.3: ScopeReassigner needs Qdrant access. Cheap to
    # construct (no model load), so we build it unconditionally.
    qdrant: QdrantClient | None = None
    try:
        qdrant = await QdrantClient.from_settings(QdrantSettings())
        log.info("dreaming_worker.qdrant_connected")
    except Exception as e:
        log.warning("dreaming_worker.qdrant_unavailable",
                    error_class=type(e).__name__, error=str(e)[:200])

    # Optional: construct the synthesis-backed predicted-question cache.
    # Only when the operator explicitly opted in — this pulls embedder
    # + retrieval + LLM dependencies, ~10-30s startup cost.
    question_cache: QuestionPreCacher | None = None
    pipeline_ctx = None
    if settings.enable_question_cache:
        try:
            question_cache, pipeline_ctx = await _build_question_cache(
                pool=pool, model_name=settings.question_cache_model,
            )
            log.info("dreaming_worker.question_cache_enabled",
                     model=settings.question_cache_model)
        except Exception as e:
            log.error("dreaming_worker.question_cache_construct_failed",
                      error_class=type(e).__name__, error=str(e)[:300])
            question_cache = None

    worker = DreamingWorker(
        pool=pool, settings=settings, question_cache=question_cache,
        qdrant=qdrant,
    )

    # Graceful shutdown on SIGINT / SIGTERM.
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig,
            lambda s=sig: asyncio.create_task(_handle_signal(s, worker)))

    try:
        await worker.start()
        return 0
    finally:
        if pipeline_ctx is not None:
            await pipeline_ctx.close()
        if qdrant is not None:
            await qdrant.close()
        await pool.close()


async def _build_question_cache(
    *, pool, model_name: str,                                # type: ignore[no-untyped-def]
) -> tuple[QuestionPreCacher, PipelineContext]:            # type: ignore[name-defined]
    """Constructs synthesis dependencies and wraps SynthesisEngine in a
    closure for QuestionPreCacher. Mirrors the AppState construction
    pattern but reuses the worker's pool to avoid double-pooling."""
    # Local imports — avoid pulling synthesis deps when the cache is off.
    from woxpas_pipeline.context import PipelineContext
    from woxpas_retrieval import RetrievalEngine
    from woxpas_synthesis import (
        CitationVerifier,
        SynthesisEngine,
        SynthesisRequest,
    )

    ctx = await PipelineContext.construct()
    embedder = await ctx.embedder()
    reranker = await ctx.reranker()
    intent_llm = None
    try:
        intent_llm = await ctx.llm_backend("summary")
    except RuntimeError:
        intent_llm = None

    retrieval = RetrievalEngine(
        pool=pool, qdrant=ctx.qdrant,
        embedder=embedder, reranker=reranker,
        intent_llm=intent_llm,
    )
    verifier = CitationVerifier(embedder)

    backend_cache: dict = {intent_llm.name: intent_llm} if intent_llm else {}
    def llm_factory(name: str | None):
        if name is None and intent_llm is not None:
            return intent_llm
        from woxpas_synthesis.engine import build_llm_backend
        resolved = name or "qwen2.5-3b-instruct-q4km"
        cached = backend_cache.get(resolved)
        if cached is not None:
            return cached
        backend = build_llm_backend(resolved)
        backend_cache[resolved] = backend
        return backend

    engine = SynthesisEngine(
        retrieval=retrieval, llm_factory=llm_factory,
        pool=pool, verifier=verifier,
    )

    async def synthesize_fn(
        query: str, tenant_id: UUID, model: str | None,
    ) -> SynthesizedPrediction:
        """Closure: wraps SynthesisEngine.synthesize() into the shape
        QuestionPreCacher expects. Cloud-policy denials raise; the
        precacher catches per-subject and continues."""
        req = SynthesisRequest(
            query_text=query,
            model_name=model or model_name,
            max_sources=8,
            max_tokens=512,
            temperature=0.0,
            persona="Be concise. Pre-cached answer; user has not asked yet.",
            allow_cloud_for_personal_data=None,    # respect tenant settings
            allow_cloud_for_sensitive_data=None,
        )
        ans = await engine.synthesize(req, tenant_id=tenant_id)
        return SynthesizedPrediction(
            answer_text=ans.answer_clean,
            citation_count=len(ans.citations or {}),
            cloud_egress=True,
            extra={
                "request_id":    str(req.request_id),
                "model_version": ans.model_version,
                "finish_reason": ans.finish_reason,
            },
        )

    return QuestionPreCacher(
        pool=pool, synthesize_fn=synthesize_fn,
        cloud_model=model_name,
    ), ctx


if TYPE_CHECKING:
    from woxpas_pipeline.context import PipelineContext


async def _handle_signal(sig: signal.Signals, worker: DreamingWorker) -> None:
    log.info("dreaming_worker.shutdown_signal", signal=sig.name)
    await worker.stop()


def main() -> None:
    """uvicorn-style entry. `woxpas-dreaming-worker` CLI calls this."""
    from woxpas_pipeline.boot import load_env
    load_env()
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
