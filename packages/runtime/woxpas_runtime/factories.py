"""Factory functions that resolve a logical model name to a concrete backend.

Every consumer (summarize stage, scope auto-assignment in CLI, future
extraction stages, future retrieval stages) goes through these factories
instead of constructing backend classes directly. That keeps the build
pluggable: swap `EMBED_MODEL_NAME` to a cloud entry → cloud backend gets
constructed with no consumer code change.

Convention: factories pick the backend class based on `ModelArtefact.backend_kind`.
Adding a new backend is two lines here plus a class somewhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from woxpas_runtime.hwprobe import HardwareInfo
from woxpas_runtime.models import (
    DEFAULT_EMBED_MODEL_NAME,
    DEFAULT_RERANKER_MODEL_NAME,
    DEFAULT_SUMMARY_MODEL_NAME,
    ModelArtefact,
    resolve_model,
)

if TYPE_CHECKING:
    from woxpas_runtime.embedder_backend import EmbedderBackend
    from woxpas_runtime.llm_backend import LlmBackend
    from woxpas_runtime.reranker_backend import RerankerBackend

log = structlog.get_logger(__name__)


def build_embedder(
    model_name: str | None = None,
    *,
    hw: HardwareInfo | None = None,
) -> EmbedderBackend:
    """Resolve a logical embedder name to a concrete EmbedderBackend.

    `model_name=None` → DEFAULT_EMBED_MODEL_NAME (currently 'bge-m3-onnx').
    Looks up the artefact in the catalog, dispatches by `backend_kind`.
    Future cloud backends are added as new branches; consumers don't change.
    """
    artefact: ModelArtefact = resolve_model(model_name or DEFAULT_EMBED_MODEL_NAME)
    kind = artefact.backend_kind

    if kind == "embedder.local_onnx":
        from woxpas_runtime.embedder import BgeM3Embedder
        log.info("runtime.embedder.build", model=artefact.model_version, kind=kind)
        return BgeM3Embedder(artefact=artefact, hw=hw)

    # Future:
    # if kind == "embedder.openai_compat":
    #     from woxpas_runtime.embedders.openai_compat import OpenAiCompatEmbedder
    #     return OpenAiCompatEmbedder(artefact=artefact)
    # if kind == "embedder.cohere":
    #     from woxpas_runtime.embedders.cohere import CohereEmbedder
    #     return CohereEmbedder(artefact=artefact)
    # if kind == "embedder.voyage":
    #     from woxpas_runtime.embedders.voyage import VoyageEmbedder
    #     return VoyageEmbedder(artefact=artefact)

    raise ValueError(
        f"unknown embedder backend_kind={kind!r} on artefact {artefact.model_version}; "
        f"register an implementation in woxpas_runtime.factories.build_embedder"
    )


def build_llm_backend(
    model_name: str | None = None,
    *,
    hw: HardwareInfo | None = None,
) -> LlmBackend:
    """Resolve a logical LLM name to a concrete LlmBackend.

    `model_name=None` → DEFAULT_SUMMARY_MODEL_NAME. Looks up the artefact in
    the catalog, dispatches by `backend_kind`. Adding a new provider is one
    new branch + one backend class — consumer code never changes.

    Caller must own the cloud-policy decision: this factory will happily
    return a cloud backend if asked. The cloud-policy gate (per tenant +
    per processing_profile) lives one layer up, in PipelineContext.
    """
    artefact: ModelArtefact = resolve_model(model_name or DEFAULT_SUMMARY_MODEL_NAME)
    kind = artefact.backend_kind

    if kind == "llm.llama_cpp_local":
        from woxpas_runtime.llm_backends.local_llama_cpp import LocalLlamaCppBackend
        log.info("runtime.llm.build", model=artefact.model_version, kind=kind)
        return LocalLlamaCppBackend(artefact=artefact, hw=hw)

    if kind == "llm.openai_compat":
        from woxpas_runtime.llm_backends.openai_compat import OpenAiCompatibleBackend
        log.info("runtime.llm.build", model=artefact.model_version, kind=kind,
                 api_base=artefact.api_base, api_model=artefact.api_model)
        return OpenAiCompatibleBackend(artefact=artefact)

    if kind == "llm.anthropic":
        from woxpas_runtime.llm_backends.anthropic import AnthropicBackend
        log.info("runtime.llm.build", model=artefact.model_version, kind=kind,
                 api_model=artefact.api_model)
        return AnthropicBackend(artefact=artefact)

    raise ValueError(
        f"unknown LLM backend_kind={kind!r} on artefact {artefact.model_version}; "
        f"register an implementation in woxpas_runtime.factories.build_llm_backend"
    )


def build_reranker(
    model_name: str | None = None,
    *,
    hw: HardwareInfo | None = None,
) -> RerankerBackend:
    """Resolve a logical reranker name to a concrete RerankerBackend.

    `model_name=None` → DEFAULT_RERANKER_MODEL_NAME (currently the INT8
    'bge-reranker-v2-m3-onnx'). Swapping models is a .env edit + catalog
    artefact bump — no code change.
    """
    artefact: ModelArtefact = resolve_model(model_name or DEFAULT_RERANKER_MODEL_NAME)
    kind = artefact.backend_kind

    if kind == "reranker.local_onnx":
        from woxpas_runtime.reranker import BgeRerankerOnnx
        log.info("runtime.reranker.build", model=artefact.model_version, kind=kind)
        return BgeRerankerOnnx(artefact=artefact, hw=hw)

    raise ValueError(
        f"unknown reranker backend_kind={kind!r} on artefact {artefact.model_version}; "
        f"register an implementation in woxpas_runtime.factories.build_reranker"
    )
