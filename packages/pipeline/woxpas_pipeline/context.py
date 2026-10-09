"""PipelineContext — the bag of clients + tenant key access every stage uses.

Lives for the duration of a worker process. Stages take the context as a
constructor arg; they don't hand-instantiate clients. This makes the wiring
testable (swap in fakes) and gives one place to retire/replace a backend.

Key access:
  ctx.session_dek(tenant_id) → bytes
The boot service is the production source of DEK material (injected via Unix
socket). For local dev, settings.DevDekSettings provides a hex-encoded DEK.
The context never persists DEK; it holds it in process memory only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from woxpas_storage import (
    ObjectStore,
    OutboxPublisher,
    PostgresPool,
    QdrantClient,
    StorageSettings,
)

from woxpas_pipeline.settings import (
    ClamAVSettings,
    DevDekSettings,
    PipelineSettings,
)

log = structlog.get_logger(__name__)


@dataclass
class PipelineContext:
    """Wiring shared by all pipeline stages."""

    pool:       PostgresPool
    qdrant:     QdrantClient
    objstore:   ObjectStore
    publisher:  OutboxPublisher
    pipeline:   PipelineSettings
    clamav:     ClamAVSettings
    storage:    StorageSettings
    _dek_by_tenant: dict[UUID, bytes]
    _dek_lock:  asyncio.Lock
    # Lazy-loaded ML runtimes. Constructed on first .embedder / .summarizer access
    # so worker startup stays fast even before the first ingest.
    _embedder:    Any | None = None
    _summarizer:  Any | None = None
    _asr:         Any | None = None
    _reranker:    Any | None = None
    # Per-role LLM backend cache. Keys: 'summary', 'extraction'.
    # A single PipelineContext can run several roles against different models.
    # Two roles pointing at the same model name share the SAME backend instance
    # (deduped via _llm_by_name) so the GGUF only loads into memory once.
    _llm_by_role: dict[str, Any] | None = None
    _llm_by_name: dict[str, Any] | None = None
    _runtime_lock: asyncio.Lock | None = None
    enable_runtime: bool = True

    # ─── construction / teardown ──────────────────────────────────────────

    @classmethod
    async def construct(
        cls,
        *,
        storage: StorageSettings | None = None,
        pipeline: PipelineSettings | None = None,
        clamav:   ClamAVSettings | None = None,
        dev:      DevDekSettings | None = None,
        enable_runtime: bool = True,
    ) -> PipelineContext:
        storage = storage or StorageSettings()
        pipeline = pipeline or PipelineSettings()
        clamav = clamav or ClamAVSettings()
        dev = dev or DevDekSettings()

        pool = await PostgresPool.from_settings(storage.postgres)
        qdr  = await QdrantClient.from_settings(storage.qdrant)
        obj  = ObjectStore(storage.objstore)
        publisher = OutboxPublisher(pool)

        deks: dict[UUID, bytes] = {}

        # ── DEK source priority ──────────────────────────────────────────
        # 1. Boot daemon Unix socket (production path). If the daemon is
        #    running, the DEK never touches .env or the filesystem.
        # 2. DevDekSettings (local dev / testing fallback).
        #
        # The daemon handles passphrase → KEK → DEK; we just request the
        # plaintext DEK over a chmod-600 socket. If the daemon isn't running,
        # we fall through silently — dev mode still works.
        boot_dek_loaded = False
        if dev.tenant_id_default is not None:
            try:
                from woxpas_boot.client import fetch_dek
                tenant_uuid = UUID(dev.tenant_id_default)
                dek_bytes = await fetch_dek(dev.tenant_id_default)
                deks[tenant_uuid] = dek_bytes
                boot_dek_loaded = True
                log.info("pipeline.context.boot_dek_loaded",
                         tenant_id=dev.tenant_id_default)
            except Exception:
                # Daemon not running or refused — fall through to dev DEK.
                log.debug("pipeline.context.boot_daemon_unavailable", exc_info=True)

        if not boot_dek_loaded and dev.dek_hex is not None and dev.tenant_id_default is not None:
            deks[UUID(dev.tenant_id_default)] = bytes.fromhex(dev.dek_hex.get_secret_value())
            log.warning("pipeline.context.dev_dek_loaded",
                        tenant_id=dev.tenant_id_default)

        return cls(
            pool=pool, qdrant=qdr, objstore=obj, publisher=publisher,
            pipeline=pipeline, clamav=clamav, storage=storage,
            _dek_by_tenant=deks, _dek_lock=asyncio.Lock(),
            _llm_by_role={}, _llm_by_name={},
            _runtime_lock=asyncio.Lock(),
            enable_runtime=enable_runtime,
        )

    async def close(self) -> None:
        await self.publisher.stop()
        await self.qdrant.close()
        await self.pool.close()
        # zeroize DEKs we hold
        async with self._dek_lock:
            for k, v in list(self._dek_by_tenant.items()):
                # bytes are immutable in Python; safest we can do is drop the ref
                self._dek_by_tenant[k] = b"\x00" * len(v)
                del self._dek_by_tenant[k]
        log.info("pipeline.context.closed")

    # ─── tenant secrets ───────────────────────────────────────────────────

    async def session_dek(self, tenant_id: UUID) -> bytes:
        """Return the DEK for a tenant.

        Raises if the boot service hasn't injected one. Workers should fail
        fast in that case — they can't decrypt the object store without it.
        """
        async with self._dek_lock:
            dek = self._dek_by_tenant.get(tenant_id)
        if dek is None:
            raise RuntimeError(
                f"no DEK loaded for tenant {tenant_id}; boot service must inject it"
            )
        return dek

    async def install_dek(self, tenant_id: UUID, dek: bytes) -> None:
        """Boot service / dev fixture installs a DEK at startup."""
        async with self._dek_lock:
            self._dek_by_tenant[tenant_id] = dek

    # ─── ML runtimes (lazy) ────────────────────────────────────────────────

    async def embedder(self):  # type: ignore[no-untyped-def]
        """Returns an EmbedderBackend resolved via the catalog. Lazy-loaded.

        Backend is whatever PipelineSettings.embed_model_name resolves to;
        defaults to BGE-M3 ONNX. Cloud backends (OpenAI, Voyage, Cohere)
        are added by registering ModelArtefacts in the catalog and extending
        woxpas_runtime.factories.build_embedder — no consumer code change.
        """
        if not self.enable_runtime:
            raise RuntimeError("runtime disabled on this context")
        if self._embedder is not None:
            return self._embedder
        assert self._runtime_lock is not None
        async with self._runtime_lock:
            if self._embedder is None:
                from woxpas_runtime import build_embedder
                self._embedder = build_embedder(
                    model_name=self.pipeline.embed_model_name,
                )
        return self._embedder

    async def summarizer(self):  # type: ignore[no-untyped-def]
        if not self.enable_runtime:
            raise RuntimeError("runtime disabled on this context")
        if self._summarizer is not None:
            return self._summarizer
        assert self._runtime_lock is not None
        async with self._runtime_lock:
            if self._summarizer is None:
                from woxpas_runtime import LlamaSummarizer
                # Settings-driven model selection. None = runtime default.
                self._summarizer = LlamaSummarizer(
                    model_name=self.pipeline.summary_model_name,
                )
        return self._summarizer

    async def llm_backend(self, role: str = "summary"):  # type: ignore[no-untyped-def]
        """Return an LlmBackend for a logical role. Lazy-loaded, cached.

        Roles map to settings fields (summary → summary_model_name, etc.).
        Two roles that resolve to the same model name share the SAME backend
        instance (deduped via _llm_by_name), so a 2GB GGUF only loads once
        even when summary + extraction both run on it. Cloud-policy gating
        is the caller's responsibility — call assert_cloud_allowed() before
        .generate() if the doc may need gating.
        """
        if not self.enable_runtime:
            raise RuntimeError("runtime disabled on this context")
        assert self._llm_by_role is not None
        assert self._llm_by_name is not None
        cached = self._llm_by_role.get(role)
        if cached is not None:
            return cached
        assert self._runtime_lock is not None
        async with self._runtime_lock:
            cached = self._llm_by_role.get(role)
            if cached is not None:
                return cached
            model_name = self._resolve_role_model(role)
            # Resolve None → the runtime's default summary model name so the
            # by-name cache key is canonical (None and "<default>" both map
            # to the same backend).
            from woxpas_runtime import DEFAULT_SUMMARY_MODEL_NAME, build_llm_backend
            canonical_name = model_name or DEFAULT_SUMMARY_MODEL_NAME
            backend = self._llm_by_name.get(canonical_name)
            if backend is None:
                backend = build_llm_backend(model_name=canonical_name)
                self._llm_by_name[canonical_name] = backend
                log.info("pipeline.llm_backend.built",
                         role=role, model=backend.name,
                         kind=backend.backend_kind, is_cloud=backend.is_cloud)
            else:
                log.info("pipeline.llm_backend.shared",
                         role=role, model=backend.name)
            self._llm_by_role[role] = backend
            return backend

    def _resolve_role_model(self, role: str) -> str | None:
        ps = self.pipeline
        explicit = {
            "summary":    ps.summary_model_name,
            "extraction": ps.extraction_model_name,
        }.get(role)
        # Roles that don't have their own setting fall through to the summary
        # model — the closest sensible default for any chat-completion role.
        return explicit if explicit is not None else ps.summary_model_name

    async def asr(self):  # type: ignore[no-untyped-def]
        if not self.enable_runtime:
            raise RuntimeError("runtime disabled on this context")
        if self._asr is not None:
            return self._asr
        assert self._runtime_lock is not None
        async with self._runtime_lock:
            if self._asr is None:
                from woxpas_runtime import WhisperAsr
                self._asr = WhisperAsr(model_name=self.pipeline.asr_model_name)
        return self._asr

    async def reranker(self):  # type: ignore[no-untyped-def]
        """Returns a RerankerBackend for retrieval. Lazy-loaded.

        Default: bge-reranker-base-onnx (multilingual, MIT-derived). Swap
        to v2-m3 in v0.2 by setting RERANKER_MODEL_NAME=bge-reranker-v2-m3-onnx.
        """
        if not self.enable_runtime:
            raise RuntimeError("runtime disabled on this context")
        if self._reranker is not None:
            return self._reranker
        assert self._runtime_lock is not None
        async with self._runtime_lock:
            if self._reranker is None:
                from woxpas_runtime import build_reranker
                self._reranker = build_reranker(
                    model_name=self.pipeline.reranker_model_name,
                )
        return self._reranker

    # ─── concurrency resolution ────────────────────────────────────────────

    def concurrency(self, stage: str) -> int:
        """Resolve a stage's concurrency: explicit setting > hwprobe default.

        stage ∈ {av, detect, chunk, summarize, embed}. 0 in settings = auto.
        """
        from woxpas_runtime import probe
        explicit = {
            "av":        self.pipeline.av_concurrency,
            "detect":    self.pipeline.detect_concurrency,
            "chunk":     self.pipeline.chunk_concurrency,
            "summarize": self.pipeline.summarize_concurrency,
            "embed":     self.pipeline.embed_concurrency,
        }.get(stage, 0)
        if explicit > 0:
            return explicit
        defaults = probe().concurrency_defaults
        return getattr(defaults, stage)

    # ─── observability ────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        from woxpas_storage import aggregate_health
        return await aggregate_health(
            pg=self.pool, qd=self.qdrant, obj=self.objstore,
        )
