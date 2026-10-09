"""RerankerBackend Protocol — contract every reranker honours.

Cross-encoder rerankers score query-document pairs jointly (vs. embedders
which encode them separately and compare via cosine). They're the gold
standard for the second pass of a hybrid retrieval pipeline: vector search
returns top-50 candidates, reranker re-orders them so the most relevant
top-K are at the top.

To add a new reranker:
  1. Add a ModelArtefact with backend_kind='reranker.local_onnx' (or new kind).
  2. Add a class implementing this Protocol in woxpas_runtime/.
  3. Add a branch in build_reranker() factory.
  4. Set RERANKER_MODEL_NAME=<name> in env or PipelineSettings.

Same code path for bge-reranker-base (v0.1) and bge-reranker-v2-m3 (v0.2)
because both are XLM-RoBERTa cross-encoders — same tokenization, same
forward signature, same logit-as-score convention.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RerankerBackend(Protocol):
    """Contract for a cross-encoder reranker. Stateless across calls;
    weights load once on first .score()."""

    @property
    def name(self) -> str:                    # logical name (e.g., "bge-reranker-base-onnx")
        ...

    @property
    def model_version(self) -> str:           # stamp for re-rank invalidation
        ...

    async def score(
        self,
        query: str,
        documents: list[str],
        *,
        batch_size: int = 16,
    ) -> list[float]:
        """Return a relevance score per document, same order as input.

        Higher score = more relevant. Score range is model-specific
        (typically unbounded raw logits in the [-15, +15] band for
        BGE rerankers); callers compare relative within a query, not
        across queries.

        `batch_size` controls how many pairs go through the model in one
        forward pass. Tunable for memory/throughput; 16 is a CPU-friendly
        default.
        """
        ...
