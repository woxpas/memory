"""BGE-M3 embedder, ONNX Runtime path — three-head (dense + sparse + ColBERT).

No torch, no transformers, no FlagEmbedding. Just onnxruntime + a HuggingFace
fast tokenizer. Cross-platform wheels (Linux x86/arm, macOS x86/arm, Windows).

Uses the Sophia-AI/bge-m3-onnx export (MIT license, numerically validated against
the original BAAI/bge-m3 via FlagEmbedding). A single ONNX file produces all
three representations in one forward pass:
  - dense    (B, 1024)      — sentence embedding, L2-normalized
  - sparse   (B, seq, 1)    — lexical token weights
  - colbert  (B, seq, 1024) — per-token vectors (CLS dropped to match reference)

When the ONNX export is dense-only (older community exports), sparse and colbert
come back empty and downstream search gracefully degrades to dense-only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
import structlog
from tokenizers import Tokenizer

from woxpas_runtime.hwprobe import HardwareInfo, probe
from woxpas_runtime.models import BGE_M3, ModelArtefact, fetch

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class EmbeddingBatch:
    """Triple-headed output for one batch of inputs.

    dense:   list of 1024-dim L2-normalized vectors (always populated).
    sparse:  per-text dict of {token_id: weight} — lexical weights for
             exact-term matching. Empty dicts when the ONNX export doesn't
             include a sparse head.
    colbert: per-text list of per-token 1024-dim vectors (CLS excluded).
             Empty lists when the ONNX export doesn't include a ColBERT head.
    """
    dense:   list[list[float]]
    sparse:  list[dict[int, float]]    # token id → weight
    colbert: list[list[list[float]]]   # per-input list of token vectors

    @property
    def size(self) -> int:
        return len(self.dense)


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=-1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return x / norms


def _mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Standard sentence-embedding mean-pooling: average non-pad token states."""
    mask = attention_mask.astype(np.float32)[..., np.newaxis]   # [B, T, 1]
    summed = (last_hidden * mask).sum(axis=1)                   # [B, H]
    counts = np.clip(mask.sum(axis=1), a_min=1.0, a_max=None)   # [B, 1]
    pooled: np.ndarray = summed / counts
    return pooled


class BgeM3Embedder:
    """ONNX-Runtime BGE-M3 embedder. Lazy-loads on first .embed() call.

    Produces dense + sparse + ColBERT embeddings from a single ONNX forward pass
    when using a three-head export (Sophia-AI/bge-m3-onnx). Degrades gracefully
    to dense-only for older single-head exports.
    """

    model_id = "bge-m3-onnx"

    def __init__(
        self,
        *,
        artefact: ModelArtefact | None = None,
        hw: HardwareInfo | None = None,
    ) -> None:
        self._hw = hw or probe()
        self._artefact = artefact if artefact is not None else BGE_M3
        self._session:   ort.InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None
        self._lock = asyncio.Lock()
        # Set after first load — which vector heads the ONNX export produces.
        self._has_sparse:  bool = False
        self._has_colbert: bool = False

    @property
    def model_version(self) -> str:
        return f"{self._artefact.name}@{self._artefact.version}"

    @property
    def dim(self) -> int:
        return 1024

    async def _ensure_loaded(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        async with self._lock:
            if self._session is not None and self._tokenizer is not None:
                return
            snapshot = await asyncio.to_thread(fetch, self._artefact)
            session, tokenizer = await asyncio.to_thread(self._open, snapshot)
            self._session = session
            self._tokenizer = tokenizer
            # Detect which heads are available from the ONNX output names.
            out_names = [o.name for o in session.get_outputs()]
            self._has_sparse  = "sparse" in out_names
            self._has_colbert = "colbert" in out_names
            log.info("embedder.loaded",
                     model=self.model_version,
                     providers=session.get_providers(),
                     heads={"dense": True, "sparse": self._has_sparse,
                            "colbert": self._has_colbert})

    def _open(self, snapshot: Path) -> tuple[ort.InferenceSession, Tokenizer]:
        # Locate the ONNX file. Common conventions:
        #   Sophia-AI/bge-m3-onnx  → model_fp32.onnx
        #   aapot/bge-m3-onnx      → model.onnx (three-head) or dense_model.onnx
        #   woxpas-ai/bge-m3-onnx  → dense_model.onnx (mirror of aapot)
        candidates = [
            snapshot / "model_fp32.onnx",
            snapshot / "model.onnx",
            snapshot / "dense_model.onnx",
            snapshot / "onnx" / "model.onnx",
        ]
        onnx_path = next((p for p in candidates if p.is_file()), None)
        if onnx_path is None:
            raise RuntimeError(
                f"no ONNX file found in {snapshot}; checked {[str(c) for c in candidates]}"
            )

        # Tokenizer may be at the snapshot root (aapot layout) or in a
        # tokenizer_hf/ subdirectory (Sophia-AI layout).
        tokenizer_candidates = [
            snapshot / "tokenizer.json",
            snapshot / "tokenizer_hf" / "tokenizer.json",
        ]
        tokenizer_path = next((p for p in tokenizer_candidates if p.is_file()), None)
        if tokenizer_path is None:
            raise RuntimeError(
                f"tokenizer.json missing in {snapshot}; checked {[str(c) for c in tokenizer_candidates]}"
            )

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = self._hw.threads
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        providers = self._available_providers()
        log.info("embedder.load_begin",
                 onnx=str(onnx_path), providers=providers,
                 device=self._hw.device_str, threads=self._hw.threads)

        session = ort.InferenceSession(
            str(onnx_path), sess_options=sess_options, providers=providers,
        )
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        return session, tokenizer

    def _available_providers(self) -> list[str]:
        installed = set(ort.get_available_providers())
        return [p for p in self._hw.onnx_providers if p in installed] or ["CPUExecutionProvider"]

    async def embed(self, texts: list[str], *, batch_size: int = 8) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(dense=[], sparse=[], colbert=[])
        await self._ensure_loaded()
        assert self._session is not None and self._tokenizer is not None

        has_sparse = self._has_sparse
        has_colbert = self._has_colbert

        def _run() -> tuple[list[list[float]], list[dict[int, float]], list[list[list[float]]]]:
            # Re-assert inside the closure — mypy doesn't carry the outer
            # narrowing across the nested-function boundary.
            assert self._tokenizer is not None
            assert self._session is not None
            dense_out:   list[list[float]]       = []
            sparse_out:  list[dict[int, float]]  = []
            colbert_out: list[list[list[float]]] = []

            for start in range(0, len(texts), batch_size):
                chunk = texts[start:start + batch_size]
                encs = self._tokenizer.encode_batch(chunk)

                # Pad to batch max length.
                max_len = max(len(e.ids) for e in encs)
                ids  = np.zeros((len(encs), max_len), dtype=np.int64)
                mask = np.zeros((len(encs), max_len), dtype=np.int64)
                for i, e in enumerate(encs):
                    n = len(e.ids)
                    ids[i, :n]  = e.ids
                    mask[i, :n] = e.attention_mask

                feed: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
                if any(i.name == "token_type_ids" for i in self._session.get_inputs()):
                    feed["token_type_ids"] = np.zeros_like(ids)

                outs = self._session.run(None, feed)

                # ── Dense (always output 0) ──────────────────────────────
                primary = outs[0]
                if primary.ndim == 3:
                    pooled = _mean_pool(primary, mask)
                elif primary.ndim == 2:
                    pooled = primary
                else:
                    raise RuntimeError(f"unexpected ONNX output shape: {primary.shape}")
                pooled = _l2_normalize(pooled.astype(np.float32))
                dense_out.extend(pooled.tolist())

                # ── Sparse (output 1 when three-head) ────────────────────
                if has_sparse and len(outs) >= 2:
                    sparse_arr = outs[1]  # (B, seq, 1)
                    for i in range(len(chunk)):
                        weights = sparse_arr[i].squeeze(-1)  # (seq,)
                        # Map token IDs → weights, dropping near-zero entries.
                        token_ids = [int(t) for t in ids[i][:len(encs[i].ids)]]
                        sp: dict[int, float] = {}
                        for tid, w in zip(token_ids, weights[:len(token_ids)], strict=False):
                            wf = float(w)
                            if abs(wf) > 1e-8:
                                sp[tid] = wf
                        sparse_out.append(sp)
                else:
                    sparse_out.extend([{} for _ in chunk])

                # ── ColBERT (output 2 when three-head) ───────────────────
                if has_colbert and len(outs) >= 3:
                    colbert_arr = outs[2]  # (B, seq, 1024)
                    for i in range(len(chunk)):
                        n_tokens = len(encs[i].ids)
                        # Drop CLS (index 0) per Sophia-AI's validation.
                        # Also drop padding tokens (where mask == 0).
                        vecs: list[list[float]] = []
                        for t in range(1, n_tokens):
                            if mask[i, t] == 0:
                                continue
                            vecs.append(colbert_arr[i, t].tolist())
                        colbert_out.append(vecs)
                else:
                    colbert_out.extend([[] for _ in chunk])

            return dense_out, sparse_out, colbert_out

        dense, sparse, colbert = await asyncio.to_thread(_run)
        return EmbeddingBatch(dense=dense, sparse=sparse, colbert=colbert)
