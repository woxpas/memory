"""App-level shared state.

Constructed once at startup and stashed on app.state so route handlers can
reach the storage clients + ingress stage without re-wiring per request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import Request
from woxpas_adapters import FileAdapter
from woxpas_pipeline import IngressStage, PipelineContext
from woxpas_resurfacing import ResurfaceEngine
from woxpas_retrieval import RetrievalEngine
from woxpas_runtime import LlmBackend, build_llm_backend
from woxpas_synthesis import CitationVerifier, SynthesisEngine

from woxpas_api.engines.conflict import ConflictEngine
from woxpas_api.engines.document_admin import DocumentAdminEngine
from woxpas_api.engines.document_read import DocumentReadEngine
from woxpas_api.engines.scope_admin import ScopeAdminEngine


@dataclass
class AppState:
    ctx: PipelineContext
    ingress: IngressStage
    file_adapter: FileAdapter
    retrieval: RetrievalEngine
    synthesis: SynthesisEngine
    resurface: ResurfaceEngine
    scope_admin: ScopeAdminEngine
    document_admin: DocumentAdminEngine
    document_read: DocumentReadEngine
    conflict: ConflictEngine

    @classmethod
    async def construct(cls) -> AppState:
        ctx = await PipelineContext.construct()
        # API does NOT start the publisher — that's the worker process's job.
        # API only needs Postgres + objstore for the sync ingress portion.
        # Pre-warm embedder + reranker so the first /recall doesn't pay
        # the ONNX model load cost (~3s). A single warmup embed fires the
        # ONNX session, loads tokenizer, and primes the inference thread.
        embedder = await ctx.embedder()
        import sys as _sys
        print("  warming embedder…", end=" ", flush=True, file=_sys.stderr)
        await embedder.embed(["warmup"], batch_size=1)
        print("✓", file=_sys.stderr)
        reranker = await ctx.reranker()
        intent_llm = None
        try:
            intent_llm = await ctx.llm_backend("summary")
        except RuntimeError:
            intent_llm = None

        retrieval = RetrievalEngine(
            pool=ctx.pool, qdrant=ctx.qdrant,
            embedder=embedder, reranker=reranker,
            intent_llm=intent_llm,
        )

        # Synthesis engine — re-uses the same embedder for verification
        # (no double-load) + the same intent_llm as the default model.
        # Per-AppState backend cache by name keeps local-model weights
        # from re-loading on second use (Pillar 1 — a 3GB GGUF re-load
        # would be 10s of cold start). Cloud backends are cheap to
        # construct but caching is uniform anyway.
        verifier = CitationVerifier(embedder)
        backend_cache: dict[str, LlmBackend] = (
            {intent_llm.name: intent_llm} if intent_llm else {}
        )

        def llm_factory(name: str | None) -> LlmBackend:
            if name is None and intent_llm is not None:
                return intent_llm
            resolved = name or "qwen2.5-3b-instruct-q4km"
            cached = backend_cache.get(resolved)
            if cached is not None:
                return cached
            backend = build_llm_backend(resolved)
            backend_cache[resolved] = backend
            return backend

        synthesis = SynthesisEngine(
            retrieval=retrieval,
            llm_factory=llm_factory,
            pool=ctx.pool,
            verifier=verifier,
        )

        resurface = ResurfaceEngine(pool=ctx.pool)

        # Phase 5a.5.0 Group A — scope CRUD (create/assign/unassign).
        # Reuses pool + qdrant + the warm embedder.
        scope_admin = ScopeAdminEngine(
            pool=ctx.pool, qdrant=ctx.qdrant, embedder=embedder,
        )

        # Phase 5a.5.0 Group B — document state changes (archive/unarchive).
        document_admin = DocumentAdminEngine(pool=ctx.pool)

        # Phase 5a.5.0 Group D — full document retrieval (chunks + decrypted
        # canonical/raw on opt-in). Holds ctx for session_dek + objstore.
        document_read = DocumentReadEngine(ctx=ctx)

        # Phase 5a.5.0 Group E — conflict list/get/resolve.
        conflict = ConflictEngine(pool=ctx.pool)

        return cls(
            ctx=ctx,
            ingress=IngressStage(ctx),
            file_adapter=FileAdapter(),
            retrieval=retrieval,
            synthesis=synthesis,
            resurface=resurface,
            scope_admin=scope_admin,
            document_admin=document_admin,
            document_read=document_read,
            conflict=conflict,
        )

    async def close(self) -> None:
        await self.ctx.close()


def app_state(request: Request) -> AppState:
    """Typed accessor for the AppState stashed on app.state at startup.

    Starlette types `request.app.state` as `Any`, so this re-asserts the
    concrete type in one place instead of leaking `Any` into every route.
    """
    return cast(AppState, request.app.state.app)
