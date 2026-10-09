"""Local llama.cpp backend.

Wraps llama-cpp-python around the LlmBackend Protocol. Lazy-loads the GGUF
on first .generate() call; subsequent calls reuse the loaded weights.

Important note carried from earlier work: pass `verbose=True` to llama.cpp
to avoid its OS-level stdout/stderr suppression that silenced concurrent
asyncio tasks. See project_v02_followups for the eventual subprocess-isolation
plan that lets us drop the verbose noise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from llama_cpp import Llama

from woxpas_runtime.hwprobe import Backend, HardwareInfo, probe
from woxpas_runtime.llm_backend import (
    LlmResponse,
    Message,
    ResponseFormat,
    StreamDelta,
    TokenUsage,
)
from woxpas_runtime.models import ModelArtefact, fetch

log = structlog.get_logger(__name__)


class LocalLlamaCppBackend:
    """LlmBackend implementation backed by llama-cpp-python."""

    backend_kind = "llm.llama_cpp_local"
    is_cloud = False

    def __init__(
        self,
        artefact: ModelArtefact,
        *,
        hw: HardwareInfo | None = None,
        n_ctx: int = 16384,
    ) -> None:
        self._artefact = artefact
        self._hw = hw or probe()
        self._n_ctx = n_ctx
        self._llm: Llama | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._artefact.name

    @property
    def model_version(self) -> str:
        return self._artefact.model_version

    async def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        async with self._lock:
            if self._llm is not None:
                return
            from llama_cpp import Llama
            path = await asyncio.to_thread(fetch, self._artefact)
            n_gpu_layers = -1 if self._hw.backend in {Backend.METAL, Backend.CUDA, Backend.ROCM} else 0
            log.info("llm.local.load_begin",
                     model=self.model_version, n_gpu_layers=n_gpu_layers)
            self._llm = await asyncio.to_thread(
                lambda: Llama(
                    model_path=str(path),
                    n_ctx=self._n_ctx,
                    n_threads=self._hw.threads,
                    n_gpu_layers=n_gpu_layers,
                    logits_all=False,
                    verbose=True,                  # see module docstring
                    seed=0,
                )
            )
            log.info("llm.local.loaded", model=self.model_version)

    async def generate(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
        timeout_s: float = 120.0,
    ) -> LlmResponse:
        await self._ensure_loaded()
        assert self._llm is not None

        # Use llama-cpp's built-in chat-template formatting via create_chat_completion.
        # That converts messages → the model's instruction format (Qwen2 / Llama-3 / etc.)
        # automatically based on the GGUF metadata. No prompt formatting in our code.
        request: dict[str, Any] = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 1.0,
        }
        if response_format == "json_object":
            request["response_format"] = {"type": "json_object"}

        def _run() -> Any:
            # Non-streaming call → a completion response dict; llama-cpp types
            # it as a response|iterator union, so return Any and index below.
            assert self._llm is not None
            return self._llm.create_chat_completion(**request)

        out = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)

        choice = out["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        usage = out.get("usage") or {}

        return LlmResponse(
            text=text,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
            model=self.name,
            backend_kind=self.backend_kind,
            raw=out,
        )

    async def generate_stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: ResponseFormat = "text",
        timeout_s: float = 120.0,
    ) -> AsyncIterator[StreamDelta]:
        """Stream tokens from llama.cpp.

        llama-cpp-python's `create_chat_completion(stream=True)` returns a
        SYNCHRONOUS iterator. We bridge it to async by running the iterator
        in a worker thread that pushes chunks onto an asyncio.Queue. The
        async generator then drains the queue and yields StreamDeltas.

        The per-chunk usage info isn't reliably emitted by llama.cpp's stream
        — we accumulate completion_tokens by counting deltas and stamp it on
        the terminal StreamDelta. prompt_tokens is unknown for streaming;
        callers needing exact accounting should use generate() instead.
        """
        await self._ensure_loaded()
        assert self._llm is not None

        request: dict[str, Any] = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 1.0,
            "stream": True,
        }
        if response_format == "json_object":
            request["response_format"] = {"type": "json_object"}

        # Sentinel marking end-of-stream from the worker thread.
        _DONE = object()
        # Sentinel for an error from the worker thread; carries the exception.
        _ERROR = object()

        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
        loop = asyncio.get_running_loop()

        def _worker() -> None:
            try:
                assert self._llm is not None
                for chunk in self._llm.create_chat_completion(**request):
                    asyncio.run_coroutine_threadsafe(
                        queue.put(chunk), loop,
                    ).result()
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    queue.put((_ERROR, e)), loop,
                ).result()
                return
            asyncio.run_coroutine_threadsafe(
                queue.put(_DONE), loop,
            ).result()

        worker_task = asyncio.create_task(asyncio.to_thread(_worker))

        completion_tokens = 0
        finish_reason: str | None = None

        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    item = await queue.get()
                    if item is _DONE:
                        break
                    if isinstance(item, tuple) and item and item[0] is _ERROR:
                        raise item[1]
                    # llama-cpp delta shape:
                    # {"choices": [{"delta": {"content": "..."}, "finish_reason": null}]}
                    choices = item.get("choices") or []
                    if not choices:
                        continue
                    c0 = choices[0]
                    delta = c0.get("delta") or {}
                    text_piece = delta.get("content") or ""
                    fr = c0.get("finish_reason")
                    if fr:
                        finish_reason = str(fr)
                    if text_piece:
                        completion_tokens += 1   # rough — counts deltas, not tokens
                        yield StreamDelta(text=text_piece, done=False)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):  # noqa: S110  # best-effort await of the cancelled llama worker task during teardown
                pass

        yield StreamDelta(
            text="",
            done=True,
            finish_reason=finish_reason or "stop",
            usage=TokenUsage(
                prompt_tokens=0,                              # not available from llama.cpp stream
                completion_tokens=completion_tokens,
                total_tokens=completion_tokens,
            ),
        )
