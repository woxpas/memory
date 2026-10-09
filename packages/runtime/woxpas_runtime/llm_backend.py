"""LlmBackend Protocol — contract every LLM backend implementation honours.

Pluggable from day one: callers (summarize stage, future extraction stages,
future agent) consume `LlmBackend` instances via the `build_llm_backend()`
factory. They never know which backend is active.

Architecture decisions:
  - OpenAI chat-completion message shape is the canonical interface
    (system/user/assistant + content). Most providers (DeepSeek, Kimi,
    Together, Groq, Anyscale, Fireworks, vLLM, Ollama, LM Studio, OpenAI
    itself) expose OpenAI-compatible endpoints — one backend covers them all.
  - Anthropic's API has a different shape (system separate, content blocks).
    A thin adapter backend converts on the wire.
  - Local llama.cpp wraps the same interface — caller never knows where the
    LLM runs.

To add a new backend:
  1. Add a ModelArtefact to MODEL_CATALOG with backend_kind="llm.<your_kind>".
  2. Add a class implementing this Protocol in woxpas_runtime/llm_backends/.
  3. Add a branch to build_llm_backend() that constructs your class.
  4. Set SUMMARY_MODEL_NAME=<your-model-name> in env or PipelineSettings.
That's it — no consumer code changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

# OpenAI chat-completion message role
MessageRole = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class Message:
    """One chat-completion message. Content is text-only in v0.1."""
    role:    MessageRole
    content: str


@dataclass(slots=True)
class TokenUsage:
    """Token accounting — used for audit logging + future cost tracking."""
    prompt_tokens:     int = 0
    completion_tokens: int = 0
    total_tokens:      int = 0


@dataclass(slots=True)
class LlmResponse:
    """Backend-agnostic response envelope."""
    text:          str                                        # the model's textual output
    finish_reason: str                                        # 'stop' | 'length' | 'content_filter' | provider-specific
    usage:         TokenUsage
    model:         str                                        # canonical name from the artefact
    backend_kind:  str                                        # which backend produced this
    raw:           dict[str, Any] | None = None               # provider's raw response (for debugging)


@dataclass(slots=True)
class StreamDelta:
    """One chunk emitted by `generate_stream`.

    The stream produces zero or more deltas with `done=False` followed by
    exactly one delta with `done=True`. The terminal delta carries the final
    `finish_reason` and `usage`; intermediate deltas leave them unset.

    `text` is the incremental token slice. Concatenate deltas to reconstruct
    the full response. The terminal delta usually has `text=""` since the
    model has already finished emitting tokens by the time finish/usage land.
    """
    text:          str
    done:          bool
    finish_reason: str | None = None
    usage:         TokenUsage | None = None


ResponseFormat = Literal["text", "json_object"]


@runtime_checkable
class LlmBackend(Protocol):
    """Contract every LLM backend honours.

    Backends are constructed by `build_llm_backend(model_name)`. They're
    lazy-loaded — the constructor is cheap; first .generate() call triggers
    model load (local) or session setup (API). After that, calls are direct.
    """

    @property
    def name(self) -> str:                       # logical name (e.g., "deepseek-chat")
        ...

    @property
    def model_version(self) -> str:              # stamp for re-run targeting
        ...

    @property
    def backend_kind(self) -> str:               # which Protocol implementation (for logs/audit)
        ...

    @property
    def is_cloud(self) -> bool:                  # gates cloud-data redaction policy
        ...

    async def generate(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
        timeout_s: float = 120.0,
    ) -> LlmResponse:
        ...

    def generate_stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
        timeout_s: float = 120.0,
    ) -> AsyncIterator[StreamDelta]:
        """Stream the model's response as token-level deltas.

        Returns an async iterator that yields zero-or-more `StreamDelta(done=False)`
        followed by exactly one `StreamDelta(done=True, finish_reason=..., usage=...)`.
        Errors raise mid-iteration; cancellation closes the upstream connection.

        Implementations should NOT swallow errors — let them propagate so the
        synthesis engine can mark the run failed and the API surface a 5xx.
        """
        ...
