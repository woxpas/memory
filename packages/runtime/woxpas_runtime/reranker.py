"""ONNX reranker backend.

Generic for any XLM-RoBERTa-architecture cross-encoder reranker (the BGE
family + multilingual variants). The class is model-agnostic — it just
loads the .onnx + tokenizer.json from a HuggingFace repo and runs the
standard forward pass:

  tokenize(query, doc) → input_ids + attention_mask → logits[0] → score

That logit IS the score; for BGE rerankers it's an unbounded relevance
signal (higher = more relevant). We don't apply sigmoid because the
orchestrator uses scores only for ordering, not as probabilities.

v0.1 default: Xenova/bge-reranker-base. Same code path will load v2-m3
when the ONNX export lands (just point the catalog artefact at the new
repo + commit hash).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from woxpas_runtime.hwprobe import HardwareInfo, probe
from woxpas_runtime.models import ModelArtefact, fetch

log = structlog.get_logger(__name__)


# Per-artefact max input length:
#   bge-reranker-base       — 514 (XLM-RoBERTa-base position embeddings)
#   bge-reranker-v2-m3      — 8192 (extended context)
# Picked from artefact.name when constructing the loader.
_DEFAULT_MAX_LENGTH = 512

_MAX_LENGTH_BY_NAME: dict[str, int] = {
    "bge-reranker-base-onnx":         512,
    "bge-reranker-v2-m3-onnx":        8192,    # int8 (default)
    "bge-reranker-v2-m3-onnx-fp16":   8192,
    "bge-reranker-v2-m3-onnx-fp32":   8192,
}


class BgeRerankerOnnx:
    """RerankerBackend implementation backed by ONNX runtime.

    Lazy-loads tokenizer + ONNX session on first .score() call. Subsequent
    calls reuse the loaded weights. Single instance shared across roles +
    per-tenant requests via the PipelineContext cache.
    """

    backend_kind = "reranker.local_onnx"

    def __init__(
        self,
        artefact: ModelArtefact,
        *,
        hw: HardwareInfo | None = None,
        max_length: int | None = None,
        onnx_subpath: str | None = None,
    ) -> None:
        self._artefact   = artefact
        self._hw         = hw or probe()
        # Resolution order for both knobs:
        #   1. explicit constructor kwarg (test override / advanced caller)
        #   2. artefact field (catalog-baked default per model)
        #   3. lookup table by artefact name (built-in defaults)
        #   4. fallback constants (works for any standard XLM-RoBERTa)
        self._max_length = (
            max_length
            or _MAX_LENGTH_BY_NAME.get(artefact.name)
            or _DEFAULT_MAX_LENGTH
        )
        self._onnx_subpath = (
            onnx_subpath
            or artefact.onnx_subpath
            or "onnx/model.onnx"
        )
        self._tokenizer = None
        self._session   = None
        self._lock      = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._artefact.name

    @property
    def model_version(self) -> str:
        return self._artefact.model_version

    async def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        async with self._lock:
            if self._session is not None:
                return

            # Snapshot the repo (gets all files including tokenizer.json
            # + onnx subdir).
            snapshot_dir = await asyncio.to_thread(fetch, self._artefact)
            log.info("reranker.load_begin",
                     model=self.model_version, snapshot=str(snapshot_dir))

            # Tokenizer (HuggingFace `tokenizers` lib — already a dep).
            from tokenizers import Tokenizer
            tok_path = snapshot_dir / "tokenizer.json"
            self._tokenizer = await asyncio.to_thread(
                Tokenizer.from_file, str(tok_path),
            )

            # ONNX session.
            import onnxruntime as ort
            providers = list(ort.get_available_providers())
            # Prefer CPU explicitly — these rerankers are fast on CPU and
            # the Apple Metal / CoreML providers can be flaky for some ops.
            if "CPUExecutionProvider" in providers:
                providers = ["CPUExecutionProvider"]

            onnx_path = snapshot_dir / self._onnx_subpath
            self._session = await asyncio.to_thread(
                ort.InferenceSession,
                str(onnx_path),
                providers=providers,
            )
            log.info("reranker.loaded",
                     model=self.model_version, providers=providers)

    async def score(
        self,
        query: str,
        documents: list[str],
        *,
        batch_size: int = 16,
    ) -> list[float]:
        if not documents:
            return []
        await self._ensure_loaded()
        assert self._tokenizer is not None
        assert self._session is not None

        # Run synchronously in a worker thread; ONNX runtime releases the
        # GIL during inference so this is genuinely concurrent with other
        # asyncio work.
        return await asyncio.to_thread(
            self._score_sync, query, documents, batch_size,
        )

    def _score_sync(
        self, query: str, documents: list[str], batch_size: int,
    ) -> list[float]:
        import numpy as np
        assert self._tokenizer is not None
        assert self._session is not None

        # Tokenize as PAIRS — XLM-RoBERTa style: <s>query</s></s>doc</s>.
        # tokenizers Encoding supports encode(pair) for this.
        scores: list[float] = []
        # Discover input names once; XLM-RoBERTa ONNX exports include
        # input_ids, attention_mask, sometimes token_type_ids.
        input_names = {i.name for i in self._session.get_inputs()}
        out_name = self._session.get_outputs()[0].name

        for start in range(0, len(documents), batch_size):
            batch_docs = documents[start : start + batch_size]
            pairs = [(query, d) for d in batch_docs]
            encs = self._tokenizer.encode_batch(pairs)

            ids   = [e.ids[: self._max_length] for e in encs]
            masks = [e.attention_mask[: self._max_length] for e in encs]

            # Pad to longest in batch.
            max_len = max(len(seq) for seq in ids)
            ids_arr   = np.zeros((len(ids), max_len), dtype=np.int64)
            masks_arr = np.zeros((len(masks), max_len), dtype=np.int64)
            for i, (seq, m) in enumerate(zip(ids, masks, strict=True)):
                ids_arr[i, : len(seq)] = seq
                masks_arr[i, : len(m)] = m

            feed: dict[str, Any] = {
                "input_ids":      ids_arr,
                "attention_mask": masks_arr,
            }
            if "token_type_ids" in input_names:
                # XLM-RoBERTa typically doesn't use token_type_ids meaningfully
                # but the ONNX graph may expect them — feed zeros.
                feed["token_type_ids"] = np.zeros_like(ids_arr)

            logits = self._session.run([out_name], feed)[0]
            # Output is (B, 1) for classification head; (B,) sometimes.
            logits = np.asarray(logits).reshape(-1)
            scores.extend(float(s) for s in logits)
        return scores
