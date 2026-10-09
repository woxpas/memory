"""woxpas-runtime — model runtimes for embedder, summarizer, ASR.

Construct once per worker; pass instances to pipeline stages via PipelineContext.
Runtimes are lazy-loaded (first call triggers fetch + load) so worker startup
stays fast even before the first ingest.
"""

from woxpas_runtime.asr import Segment, TranscriptionResult, WhisperAsr
from woxpas_runtime.embedder import BgeM3Embedder, EmbeddingBatch
from woxpas_runtime.embedder_backend import EmbedderBackend
from woxpas_runtime.factories import build_embedder, build_llm_backend, build_reranker
from woxpas_runtime.hwprobe import Backend, HardwareInfo, probe
from woxpas_runtime.llm_backend import (
    LlmBackend,
    LlmResponse,
    Message,
    ResponseFormat,
    StreamDelta,
    TokenUsage,
)
from woxpas_runtime.llm_backends import (
    AnthropicBackend,
    LocalLlamaCppBackend,
    OpenAiCompatibleBackend,
)
from woxpas_runtime.models import (
    ANTHROPIC_HAIKU,
    ANTHROPIC_OPUS,
    ANTHROPIC_SONNET,
    ANTHROPIC_SONNET_THINKING,
    BGE_M3,
    BGE_M3_ONNX,
    BGE_RERANKER_BASE,
    BGE_RERANKER_V2_M3_FP16,
    BGE_RERANKER_V2_M3_FP32,
    BGE_RERANKER_V2_M3_INT8,
    DEEPSEEK_CHAT,
    DEEPSEEK_REASONER,
    DEFAULT_ASR_MODEL_NAME,
    DEFAULT_EMBED_MODEL_NAME,
    DEFAULT_RERANKER_MODEL_NAME,
    DEFAULT_SUMMARY_MODEL_NAME,
    KIMI_K2,
    MODEL_CATALOG,
    OPENAI_O3_MINI,
    QWEN_3B_GGUF,
    QWEN_15B_GGUF,
    WHISPER_SMALL_CT2,
    ModelArtefact,
    ModelClass,
    fetch,
    register_model,
    resolve_model,
)
from woxpas_runtime.prompt_safety import (
    SOURCE_END,
    SOURCE_START,
    ContentInjectionError,
    build_hardened_system_prompt,
    detect_known_injection_phrases,
    expect_json_object,
    validate_no_delimiter_collision,
    wrap_source,
)
from woxpas_runtime.reranker import BgeRerankerOnnx
from woxpas_runtime.reranker_backend import RerankerBackend
from woxpas_runtime.summarizer import LlamaSummarizer, SummaryResult, build_prompt, tiny_doc_summary

__all__ = [
    "ANTHROPIC_HAIKU",
    "ANTHROPIC_OPUS",
    "ANTHROPIC_SONNET",
    "ANTHROPIC_SONNET_THINKING",
    "BGE_M3",
    "BGE_M3_ONNX",
    "BGE_RERANKER_BASE",
    "BGE_RERANKER_V2_M3_FP16",
    "BGE_RERANKER_V2_M3_FP32",
    "BGE_RERANKER_V2_M3_INT8",
    "DEEPSEEK_CHAT",
    "DEEPSEEK_REASONER",
    "DEFAULT_ASR_MODEL_NAME",
    "DEFAULT_EMBED_MODEL_NAME",
    "DEFAULT_RERANKER_MODEL_NAME",
    "DEFAULT_SUMMARY_MODEL_NAME",
    "KIMI_K2",
    "MODEL_CATALOG",
    "OPENAI_O3_MINI",
    "QWEN_3B_GGUF",
    "QWEN_15B_GGUF",
    "SOURCE_END",
    "SOURCE_START",
    "WHISPER_SMALL_CT2",
    "AnthropicBackend",
    # Hardware
    "Backend",
    "BgeM3Embedder",
    "BgeRerankerOnnx",
    # Prompt-injection-resistant prompt construction (used by ALL LLM call sites)
    "ContentInjectionError",
    # Embedder Protocol + factory + concrete backends
    "EmbedderBackend",
    "EmbeddingBatch",
    "HardwareInfo",
    # Legacy summarizer wrapper (kept until summarize stage is refactored to use LlmBackend)
    "LlamaSummarizer",
    # LLM Protocol + factory + concrete backends
    "LlmBackend",
    "LlmResponse",
    "LocalLlamaCppBackend",
    "Message",
    # Catalog
    "ModelArtefact",
    "ModelClass",
    "OpenAiCompatibleBackend",
    # Reranker Protocol + factory + concrete backends
    "RerankerBackend",
    "ResponseFormat",
    "Segment",
    "StreamDelta",
    "SummaryResult",
    "TokenUsage",
    "TranscriptionResult",
    # ASR
    "WhisperAsr",
    "build_embedder",
    "build_hardened_system_prompt",
    "build_llm_backend",
    "build_prompt",
    "build_reranker",
    "detect_known_injection_phrases",
    "expect_json_object",
    "fetch",
    "probe",
    "register_model",
    "resolve_model",
    "tiny_doc_summary",
    "validate_no_delimiter_collision",
    "wrap_source",
]
