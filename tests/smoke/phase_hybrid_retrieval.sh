#!/usr/bin/env bash
# Hybrid retrieval smoke test — verifies three-head (dense + sparse + ColBERT)
# embedder contract, RRF fusion, and graceful degradation.
#
# Does NOT require Docker infra or models — pure Python import + unit checks.
# Run:  bash tests/smoke/phase_hybrid_retrieval.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

PASS=0
FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# ─── 1. Model artefact points to three-head ONNX ──────────────────────────
note "1. Model artefact — Sophia-AI three-head export"
RESULT=$(uv run python -c "
from woxpas_runtime.models import BGE_M3_ONNX
print(f'{BGE_M3_ONNX.name} v{BGE_M3_ONNX.version} repo={BGE_M3_ONNX.repo}')
")
echo "  $RESULT"
if echo "$RESULT" | grep -q "Sophia-AI/bge-m3-onnx"; then
  ok "artefact points to Sophia-AI (MIT, three-head)"
else
  bad "artefact does not point to Sophia-AI"
fi

# ─── 2. EmbeddingBatch contract ───────────────────────────────────────────
note "2. EmbeddingBatch — three-head contract"
uv run python -c "
from woxpas_runtime.embedder import EmbeddingBatch
import numpy as np

# Dense-only batch (old ONNX / graceful degradation)
b = EmbeddingBatch(dense=[[0.5]*1024], sparse=[{}], colbert=[[]])
assert b.size == 1
assert b.sparse[0] == {}
assert b.colbert[0] == []

# Full three-head batch
b2 = EmbeddingBatch(
    dense=[[0.1]*1024],
    sparse=[{123: 0.5, 456: 0.3}],
    colbert=[[[0.01]*1024]*5],   # 5 tokens
)
assert b2.size == 1
assert len(b2.sparse[0]) == 2
assert len(b2.colbert[0]) == 5
assert len(b2.colbert[0][0]) == 1024
print('OK')
" && ok "EmbeddingBatch accepts all three heads" || bad "EmbeddingBatch contract broken"

# ─── 3. L2 normalization + mean pooling ───────────────────────────────────
note "3. Math helpers"
uv run python -c "
from woxpas_runtime.embedder import _l2_normalize, _mean_pool
import numpy as np

x = np.array([[3.0, 4.0]], dtype=np.float32)
n = _l2_normalize(x)
assert abs(np.linalg.norm(n[0]) - 1.0) < 1e-6

hidden = np.array([[[1.0, 2.0], [3.0, 4.0], [0.0, 0.0]]], dtype=np.float32)
mask   = np.array([[1, 1, 0]], dtype=np.float32)
pooled = _mean_pool(hidden, mask)
assert abs(pooled[0, 0] - 2.0) < 1e-6
assert abs(pooled[0, 1] - 3.0) < 1e-6
print('OK')
" && ok "l2_normalize + mean_pool correct" || bad "math helpers broken"

# ─── 4. RRF multi-head fusion ─────────────────────────────────────────────
note "4. RRF — multi-head dedup boosts cross-head matches"
uv run python -c "
from woxpas_retrieval.backends.vector import _reciprocal_rank_fusion

# Chunk 'a' appears in both dense and sparse lists → should outrank 'b'
dense  = [{'kind': 'chunk', 'chunk_id': 'a', 'doc_id': 'd1', 'raw_score': 0.9},
          {'kind': 'chunk', 'chunk_id': 'b', 'doc_id': 'd2', 'raw_score': 0.7}]
sparse = [{'kind': 'chunk', 'chunk_id': 'a', 'doc_id': 'd1', 'raw_score': 0.8}]
fused = _reciprocal_rank_fusion([dense, sparse])
a_score = next(f['fused_score'] for f in fused if f['chunk_id'] == 'a')
b_score = next(f['fused_score'] for f in fused if f['chunk_id'] == 'b')
assert a_score > b_score, f'a={a_score} <= b={b_score}'
print(f'  a={a_score:.4f} > b={b_score:.4f}')
" && ok "RRF dedup boosts multi-head matches" || bad "RRF fusion broken"

# ─── 5. RRF empty-list handling ───────────────────────────────────────────
note "5. RRF — empty sparse/colbert lists (graceful degradation)"
uv run python -c "
from woxpas_retrieval.backends.vector import _reciprocal_rank_fusion

dense = [{'kind': 'chunk', 'chunk_id': 'x', 'doc_id': 'd1', 'raw_score': 0.5}]
fused = _reciprocal_rank_fusion([dense, [], []])  # sparse + colbert empty
assert len(fused) == 1
assert fused[0]['fused_score'] > 0
print('OK')
" && ok "empty lists don't break RRF" || bad "RRF chokes on empty lists"

# ─── 6. Pipeline stage import ─────────────────────────────────────────────
note "6. EmbedIndexStage imports cleanly"
uv run python -c "
from woxpas_pipeline.embed.stage import EmbedIndexStage
print('OK')
" && ok "EmbedIndexStage loads" || bad "EmbedIndexStage import failed"

# ─── 7. Vector backend import ─────────────────────────────────────────────
note "7. VectorBackend imports cleanly"
uv run python -c "
from woxpas_retrieval.backends.vector import VectorBackend
print('OK')
" && ok "VectorBackend loads" || bad "VectorBackend import failed"

# ─── Summary ──────────────────────────────────────────────────────────────
echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
