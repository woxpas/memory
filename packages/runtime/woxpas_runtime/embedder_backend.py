"""EmbedderBackend Protocol — contract every embedder implementation honours.

Pluggable from day one: callers (summarize stage, scope auto-assignment in CLI,
extraction stages, future retrieval stages) consume `EmbedderBackend` instances
via the `build_embedder()` factory. They never know which backend is active.

To add a new backend (e.g., OpenAI's text-embedding-3-large):
  1. Add a ModelArtefact to MODEL_CATALOG with backend_kind="embedder.openai_compat".
  2. Add a class implementing this Protocol in woxpas_runtime/embedders/.
  3. Add a branch to build_embedder() that constructs your class for that kind.
  4. Set EMBED_MODEL_NAME=<your-model-name> in env or PipelineSettings.
That's it — no consumer code changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from woxpas_runtime.embedder import EmbeddingBatch  # shared dataclass


@runtime_checkable
class EmbedderBackend(Protocol):
    """Single async method, plus identity properties stamped on every artefact
    we produce so re-runs can target the right model_version selectively.
    """

    model_id: str
    """Stable logical model name (e.g. 'bge-m3-onnx'), without the version."""

    @property
    def model_version(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(
        self,
        texts: list[str],
        *,
        batch_size: int = 8,
    ) -> EmbeddingBatch: ...
