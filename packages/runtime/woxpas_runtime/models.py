"""Model artefact registry + downloader.

We resolve model weights via huggingface-hub into a local cache. In production
the boot service pre-fetches into the encrypted volume so the offline path
never reaches the network at runtime. For dev, first call hits HF.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog
from huggingface_hub import hf_hub_download, snapshot_download

log = structlog.get_logger(__name__)


def _models_root() -> Path:
    return Path(os.environ.get("WOXPAS_MODELS_DIR", "/var/woxpas/models")) \
        if os.path.isdir("/var/woxpas") \
        else Path(os.environ.get("WOXPAS_MODELS_DIR", "./models"))


# Reasoning vs. standard chat models accept a different request shape:
#   standard  — accepts temperature, top_p, max_tokens, response_format
#   reasoning — OpenAI o-series + DeepSeek-R1: temperature must be 1.0 (or
#               omitted), uses max_completion_tokens, may take reasoning_effort
#             — Anthropic thinking models: must enable a `thinking` block,
#               temperature must be 1.0, may emit thinking_delta stream events
# Backends branch on this value to build the correct request.
ModelClass = Literal["standard", "reasoning"]


@dataclass(slots=True, frozen=True)
class ModelArtefact:
    """A model on disk OR a remote API endpoint, plus the version stamp.

    `backend_kind` selects which backend implementation will load this model.
    Factories (build_embedder, build_summarizer, ...) read this field.

    Local-model fields (repo/file/revision) are populated when backend_kind is
    one of the local kinds. API fields (api_base/api_model/api_key_env) are
    populated for hosted-API kinds. A future cloud-backed BGE entry might be:

        ModelArtefact(
            name="openai-text-embedding-3-large", version="1",
            backend_kind="embedder.openai_compat",
            api_base="https://api.openai.com/v1",
            api_model="text-embedding-3-large",
            api_key_env="WOXPAS_KEY_OPENAI",
        )

    The 4th pillar (sovereign/adaptable) means a user can register ANY model
    they have an endpoint for — including reasoning models (o3, DeepSeek-R1,
    Claude with extended thinking). Set `model_class="reasoning"` and add
    provider-specific knobs via `extra_request_kwargs` (merged into the
    request body). Examples:

        OpenAI o3-mini:
          model_class="reasoning",
          extra_request_kwargs={"reasoning_effort": "medium"}

        Claude Sonnet w/ extended thinking:
          model_class="reasoning",
          extra_request_kwargs={
              "thinking": {"type": "enabled", "budget_tokens": 8000}
          }
    """
    name: str                       # logical name
    version: str                    # bumped on weights/prompt change
    backend_kind: str               # see _KNOWN_BACKEND_KINDS below

    # Local-model fields (None for hosted-API artefacts)
    repo:     str | None = None     # HuggingFace repo
    file:     str | None = None     # specific file (gguf/onnx) or None for snapshot
    revision: str | None = None     # pinned commit hash

    # Subpath inside a downloaded snapshot when the snapshot has multiple
    # candidate weight files (e.g., fp32 / fp16 / quantized variants of an
    # ONNX export). Loader joins this onto the snapshot dir. None means
    # backend's default ('onnx/model.onnx' for ONNX backends).
    onnx_subpath: str | None = None

    # Hosted-API fields (None for local artefacts)
    api_base:     str | None = None
    api_model:    str | None = None
    api_key_env:  str | None = None

    # Request-shape selector + extras. Default is the standard chat model
    # shape we've used since v0.1.
    model_class:          ModelClass            = "standard"
    extra_request_kwargs: dict[str, Any] | None = None

    @property
    def model_version(self) -> str:
        return f"{self.name}@{self.version}"


# Recognized backend_kind values. Factories raise on unknown kinds. Adding a
# new backend = add a string here + an implementation in the factory.
_KNOWN_BACKEND_KINDS = frozenset({
    # Embedder backends
    "embedder.local_onnx",          # current — onnxruntime + tokenizers
    "embedder.openai_compat",       # future — OpenAI / DeepSeek / Together / etc.
    "embedder.cohere",              # future
    "embedder.voyage",              # future
    # Summarizer / chat-completion backends
    "llm.llama_cpp_local",          # current — local GGUF via llama-cpp-python
    "llm.openai_compat",            # future — OpenAI / DeepSeek / Kimi / etc.
    "llm.anthropic",                # future
    # ASR backends
    "asr.faster_whisper_local",     # current
    "asr.openai_compat",            # future
    # Reranker backends
    "reranker.local_onnx",          # current — XLM-RoBERTa cross-encoders via ONNX
    "reranker.cohere",              # future — Cohere Rerank API
    "reranker.voyage",              # future
})


def _validate_backend_kind(kind: str) -> None:
    if kind not in _KNOWN_BACKEND_KINDS:
        log.warning("runtime.model.unknown_backend_kind",
                    backend_kind=kind,
                    known=sorted(_KNOWN_BACKEND_KINDS))


# ─── Default catalog ────────────────────────────────────────────────────────

# ─── Model catalog ──────────────────────────────────────────────────────────
#
# Logical name → ModelArtefact. Built-in entries below; users can register
# additional artefacts at runtime via register_model() (e.g., a custom GGUF
# they downloaded themselves, or a quantization variant we don't ship).
#
# Hardcoded model selection would be the death of a sovereign-memory product.
# Every place that needs "the summary model" / "the embedder" should resolve
# through resolve_model(name) so the configured model is what runs.
#
# Convention for `name`: short, lowercase, hyphenated, includes quantization
# tag if applicable. Names are stable forever; bumping `version` when weights
# or prompts change is what triggers selective re-processing.

# Embedders
# Sophia-AI/bge-m3-onnx — MIT-licensed three-head ONNX export (dense + sparse +
# ColBERT) validated numerically identical to the original BAAI/bge-m3 via
# FlagEmbedding. Single model_fp32.onnx file with external data. FP32 only;
# int8 was tested and rejected (dense cosine dropped to 0.985).
BGE_M3_ONNX = ModelArtefact(
    name="bge-m3-onnx",
    version="2",                          # bumped — new repo, new heads
    backend_kind="embedder.local_onnx",
    repo="Sophia-AI/bge-m3-onnx",
    file=None,                            # full snapshot — ONNX + tokenizer
)
# Backward-compat alias used by some imports.
BGE_M3 = BGE_M3_ONNX

# Summarizers / reasoners
QWEN_15B_GGUF = ModelArtefact(
    name="qwen2.5-1.5b-instruct-q4km",
    version="1",
    backend_kind="llm.llama_cpp_local",
    repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
    file="qwen2.5-1.5b-instruct-q4_k_m.gguf",
)
QWEN_3B_GGUF = ModelArtefact(
    name="qwen2.5-3b-instruct-q4km",
    version="1",
    backend_kind="llm.llama_cpp_local",
    repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
    file="qwen2.5-3b-instruct-q4_k_m.gguf",
)

# ASR — faster-whisper / CTranslate2 format
WHISPER_SMALL_CT2 = ModelArtefact(
    name="whisper-small-ct2",
    version="1",
    backend_kind="asr.faster_whisper_local",
    repo="Systran/faster-whisper-small",
    file=None,    # full snapshot — CT2 model dir
)

# Rerankers — XLM-RoBERTa cross-encoders via ONNX
# v0.1: Xenova/bge-reranker-base (multilingual, MIT-derived from BAAI/bge-reranker-base).
# v0.2 swap target: bge-reranker-v2-m3-onnx (better multilingual; ONNX export
# pending community/self-hosted publish). Same backend_kind + same loader code,
# just point this artefact at the new repo + revision.
BGE_RERANKER_BASE = ModelArtefact(
    name="bge-reranker-base-onnx",
    version="1",
    backend_kind="reranker.local_onnx",
    repo="Xenova/bge-reranker-base",
    file=None,                          # full snapshot — ONNX + tokenizer
)

# v2-m3 self-hosted ONNX export (woxpas-ai org). Three quantization variants
# share one repo; default is INT8 (544 MB).
#
# Why int8 default: for RANKING (relative order, not absolute scores) the
# max_delta=0.504 vs PyTorch is well below typical relevant-vs-irrelevant
# logit gaps (~5+). Cosine similarity to PyTorch is 0.9996 — functionally
# identical for our use. int8 is 4× smaller than fp32 and ~2-3× faster on
# CPU due to SIMD acceleration. Pillars 1 + 4 win; pillar 2 unaffected.
#
# fp16 / fp32 are addressable for advanced callers (cross-query absolute
# score comparisons, evaluation runs).
BGE_RERANKER_V2_M3_INT8 = ModelArtefact(
    name="bge-reranker-v2-m3-onnx",
    version="1",
    backend_kind="reranker.local_onnx",
    repo="woxpas-ai/bge-reranker-v2-m3-onnx",
    revision="c44ebc43de724ae8816668bb44d2e728e17faa18",   # pinned for reproducibility
    file=None,
    onnx_subpath="onnx/model_quantized.onnx",
)
BGE_RERANKER_V2_M3_FP16 = ModelArtefact(
    name="bge-reranker-v2-m3-onnx-fp16",
    version="1",
    backend_kind="reranker.local_onnx",
    repo="woxpas-ai/bge-reranker-v2-m3-onnx",
    revision="c44ebc43de724ae8816668bb44d2e728e17faa18",
    file=None,
    onnx_subpath="onnx/model_fp16.onnx",
)
BGE_RERANKER_V2_M3_FP32 = ModelArtefact(
    name="bge-reranker-v2-m3-onnx-fp32",
    version="1",
    backend_kind="reranker.local_onnx",
    repo="woxpas-ai/bge-reranker-v2-m3-onnx",
    revision="c44ebc43de724ae8816668bb44d2e728e17faa18",
    file=None,
    onnx_subpath="onnx/model.onnx",
)

# ─── Cloud LLM artefacts ─────────────────────────────────────────────────────
#
# Convention: name is "<provider>:<api_model>" so users can swap models by
# editing the api_model field of an entry — or registering a fresh artefact
# pointing at any other model the provider offers — without code changes.
# These are templates; provider may rename models over time, so users override
# `api_model` on their own ModelArtefact via register_model() if needed.

# DeepSeek — OpenAI-compatible. Any DeepSeek model id works.
DEEPSEEK_CHAT = ModelArtefact(
    name="deepseek-chat",
    version="1",
    backend_kind="llm.openai_compat",
    api_base="https://api.deepseek.com",
    api_model="deepseek-chat",
    api_key_env="WOXPAS_KEY_DEEPSEEK",
)
DEEPSEEK_REASONER = ModelArtefact(
    name="deepseek-reasoner",
    version="1",
    backend_kind="llm.openai_compat",
    api_base="https://api.deepseek.com",
    api_model="deepseek-reasoner",
    api_key_env="WOXPAS_KEY_DEEPSEEK",
    model_class="reasoning",       # R1 doesn't accept temperature/top_p
)

# OpenAI o-series (o3-mini etc.) — reasoning shape: no temperature, uses
# max_completion_tokens. User can register their own variant pointing at
# a different api_model + reasoning_effort via register_model().
OPENAI_O3_MINI = ModelArtefact(
    name="openai-o3-mini",
    version="1",
    backend_kind="llm.openai_compat",
    api_base="https://api.openai.com/v1",
    api_model="o3-mini",
    api_key_env="WOXPAS_KEY_OPENAI",
    model_class="reasoning",
    extra_request_kwargs={"reasoning_effort": "medium"},
)

# Kimi / Moonshot — also OpenAI-compatible.
KIMI_K2 = ModelArtefact(
    name="kimi-k2",
    version="1",
    backend_kind="llm.openai_compat",
    api_base="https://api.moonshot.cn/v1",
    api_model="moonshot-v1-32k",
    api_key_env="WOXPAS_KEY_KIMI",
)

# Anthropic Claude — distinct API shape (handled by AnthropicBackend).
ANTHROPIC_SONNET = ModelArtefact(
    name="claude-sonnet-4-6",
    version="1",
    backend_kind="llm.anthropic",
    api_base="https://api.anthropic.com",
    api_model="claude-sonnet-4-6",
    api_key_env="WOXPAS_KEY_ANTHROPIC",
)
ANTHROPIC_HAIKU = ModelArtefact(
    name="claude-haiku-4-5",
    version="1",
    backend_kind="llm.anthropic",
    api_base="https://api.anthropic.com",
    api_model="claude-haiku-4-5",
    api_key_env="WOXPAS_KEY_ANTHROPIC",
)
ANTHROPIC_OPUS = ModelArtefact(
    name="claude-opus-4-7",
    version="1",
    backend_kind="llm.anthropic",
    api_base="https://api.anthropic.com",
    api_model="claude-opus-4-7",
    api_key_env="WOXPAS_KEY_ANTHROPIC",
)

# Same Sonnet model as ANTHROPIC_SONNET but with extended thinking enabled.
# Distinct logical name lets users pick "thinking-mode" via SUMMARY_MODEL_NAME
# without code changes. Budget defaults to 8K tokens — adjust per-tenant.
ANTHROPIC_SONNET_THINKING = ModelArtefact(
    name="claude-sonnet-4-6-thinking",
    version="1",
    backend_kind="llm.anthropic",
    api_base="https://api.anthropic.com",
    api_model="claude-sonnet-4-6",
    api_key_env="WOXPAS_KEY_ANTHROPIC",
    model_class="reasoning",
    extra_request_kwargs={
        "thinking": {"type": "enabled", "budget_tokens": 8000},
    },
)


# Built-in catalog. Keyed by logical name.
MODEL_CATALOG: dict[str, ModelArtefact] = {
    BGE_M3_ONNX.name:        BGE_M3_ONNX,
    QWEN_15B_GGUF.name:      QWEN_15B_GGUF,
    QWEN_3B_GGUF.name:       QWEN_3B_GGUF,
    WHISPER_SMALL_CT2.name:  WHISPER_SMALL_CT2,
    BGE_RERANKER_BASE.name:            BGE_RERANKER_BASE,
    BGE_RERANKER_V2_M3_FP16.name:      BGE_RERANKER_V2_M3_FP16,
    BGE_RERANKER_V2_M3_FP32.name:      BGE_RERANKER_V2_M3_FP32,
    BGE_RERANKER_V2_M3_INT8.name:      BGE_RERANKER_V2_M3_INT8,
    DEEPSEEK_CHAT.name:                DEEPSEEK_CHAT,
    DEEPSEEK_REASONER.name:            DEEPSEEK_REASONER,
    OPENAI_O3_MINI.name:               OPENAI_O3_MINI,
    KIMI_K2.name:                      KIMI_K2,
    ANTHROPIC_SONNET.name:             ANTHROPIC_SONNET,
    ANTHROPIC_SONNET_THINKING.name:    ANTHROPIC_SONNET_THINKING,
    ANTHROPIC_HAIKU.name:              ANTHROPIC_HAIKU,
    ANTHROPIC_OPUS.name:               ANTHROPIC_OPUS,
}

# Defaults — what each role resolves to when settings don't override.
DEFAULT_EMBED_MODEL_NAME:    str = BGE_M3_ONNX.name
DEFAULT_SUMMARY_MODEL_NAME:  str = QWEN_15B_GGUF.name
DEFAULT_ASR_MODEL_NAME:      str = WHISPER_SMALL_CT2.name
DEFAULT_RERANKER_MODEL_NAME: str = BGE_RERANKER_V2_M3_INT8.name


def register_model(artefact: ModelArtefact) -> None:
    """Add (or replace) a custom model in the catalog. Future settings UI / boot
    config can call this at startup to expose user-supplied models without code
    changes. Re-registration with the same name + version is idempotent."""
    existing = MODEL_CATALOG.get(artefact.name)
    if existing is not None and existing != artefact:
        log.info("runtime.model.replaced", name=artefact.name,
                 old_version=existing.version, new_version=artefact.version)
    MODEL_CATALOG[artefact.name] = artefact


def resolve_model(name: str) -> ModelArtefact:
    """Look up a model artefact by logical name."""
    art = MODEL_CATALOG.get(name)
    if art is None:
        raise KeyError(
            f"unknown model name {name!r}; "
            f"known: {sorted(MODEL_CATALOG)}"
        )
    return art


def _allow_patterns_for(artefact: ModelArtefact) -> list[str] | None:
    """Build HF allow_patterns so snapshot_download only pulls what THIS
    artefact actually loads. Without this, repos that ship multiple
    quantization variants (e.g., woxpas-ai/bge-reranker-v2-m3-onnx ships
    fp32 + fp16 + int8 = ~3.7 GB) bloat the local cache by 6-7×.

    Default-None when there's no onnx_subpath — the snapshot fetches
    everything, which is correct for repos like aapot/bge-m3-onnx that
    ship a single ONNX variant alongside tokenizer + config.
    """
    if not artefact.onnx_subpath:
        return None
    base = artefact.onnx_subpath
    return [
        # Tokenizer + config files (always needed for ONNX inference)
        "*.json", "*.model", "tokenizer*", "special_tokens*", "config*",
        "README*", "LICENSE*",
        # The specific ONNX file we want
        base,
        # External weight data may be named two ways depending on exporter:
        #   {base}.data    — e.g., model_fp16.onnx → model_fp16.onnx.data
        #   {base}_data    — e.g., model.onnx     → model.onnx_data
        f"{base}.data",
        f"{base}_data",
    ]


def estimate_download_bytes(artefact: ModelArtefact) -> int:
    """Estimate total bytes to download for an artefact (one HF API call).

    Filters siblings by:
      - the specific filename if artefact.file is set
      - allow_patterns derived from onnx_subpath if it's a snapshot artefact
      - all siblings otherwise

    Returns 0 for hosted-API artefacts (no download). Returns 0 + warning
    if the API call fails (offline / repo gone) — caller continues with
    best-effort estimates.
    """
    if not artefact.repo:
        return 0
    try:
        from huggingface_hub import HfApi
        info = HfApi().repo_info(
            repo_id=artefact.repo,
            revision=artefact.revision,
            files_metadata=True,
        )
    except Exception as e:
        log.warning("runtime.model.size_estimate_failed",
                    model=artefact.model_version, error=str(e))
        return 0

    # siblings is present on ModelInfo/DatasetInfo/SpaceInfo (and may be None);
    # getattr keeps the broad repo_info() union from leaking type errors here.
    siblings = getattr(info, "siblings", None) or []

    if artefact.file:
        for sib in siblings:
            if sib.rfilename == artefact.file:
                return sib.size or 0
        return 0

    patterns = _allow_patterns_for(artefact)
    if patterns is None:
        return sum(sib.size or 0 for sib in siblings)

    import fnmatch
    total = 0
    for sib in siblings:
        for pat in patterns:
            if fnmatch.fnmatch(sib.rfilename, pat):
                total += sib.size or 0
                break
    return total


def cached_bytes(artefact: ModelArtefact) -> int:
    """Sum of bytes currently on disk for this artefact's snapshot dir.
    Returns 0 if not yet fetched."""
    cache = _models_root()
    if not cache.exists():
        return 0
    if not artefact.repo:
        return 0
    # HF cache structure: models--{org}--{repo}/snapshots/{revision}/
    safe = artefact.repo.replace("/", "--")
    repo_dir = cache / f"models--{safe}"
    if not repo_dir.exists():
        return 0
    total = 0
    for f in repo_dir.rglob("*"):
        if f.is_file() and not f.is_symlink():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def fetch(artefact: ModelArtefact) -> Path:
    """Resolve to a local path, downloading if missing.

    For single-file artefacts (artefact.file set) → hf_hub_download.
    For snapshot artefacts → snapshot_download with allow_patterns derived
    from the artefact so we don't pull unused quantization variants.
    """
    if artefact.repo is None:
        raise ValueError(
            f"artefact {artefact.model_version!r} has no repo to fetch from"
        )
    cache = _models_root()
    cache.mkdir(parents=True, exist_ok=True)
    log.info("runtime.model.fetch_begin", model=artefact.model_version, cache=str(cache))
    if artefact.file:
        path = Path(hf_hub_download(
            repo_id=artefact.repo,
            filename=artefact.file,
            revision=artefact.revision,
            cache_dir=str(cache),
        ))
    else:
        patterns = _allow_patterns_for(artefact)
        path = Path(snapshot_download(
            repo_id=artefact.repo,
            revision=artefact.revision,
            cache_dir=str(cache),
            allow_patterns=patterns,
        ))
    log.info("runtime.model.fetch_done", model=artefact.model_version, path=str(path))
    return path
