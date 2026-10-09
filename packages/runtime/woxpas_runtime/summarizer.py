"""Local LLM summarizer (Qwen2.5-1.5B Q4_K_M via llama.cpp).

Single generic prompt for v0.1 — produces {summary, topics} for any document.
Profile-aware behavior is reserved for the extraction pipeline (built later);
the summarize stage doesn't need it. The same prompt works well across thoughts,
voice notes, lyrics, contracts, papers, and code at this granularity.

Tiny-doc shortcut: if a document is shorter than ~200 chars, the doc IS the
summary — skip the LLM call entirely (saves ~10s per ingest of a one-line
thought).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from llama_cpp import Llama

from woxpas_runtime.hwprobe import Backend, HardwareInfo, probe
from woxpas_runtime.models import (
    DEFAULT_SUMMARY_MODEL_NAME,
    ModelArtefact,
    fetch,
    resolve_model,
)

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SummaryResult:
    summary: str
    topics: list[str]
    raw: str
    model_version: str
    profile_version: str


_PROFILE_VERSION = "0.1.0"   # bump on prompt change to trigger targeted re-summary

_GENERIC_SYSTEM = (
    "You are a careful summarizer. Reply ONLY with a single JSON object of the "
    'form {"summary": "...", "topics": ["...", ...]}. No markdown, no commentary. '
    "summary: 1-3 sentences capturing what this document is about. "
    "topics: 3-5 short noun phrases (1-3 words each) capturing the main subjects."
)


def build_prompt(body: str, *, max_chars: int = 24000) -> str:
    """Single generic prompt. max_chars chosen for speed (24K chars ≈ 7.5K tokens,
    well within Qwen-1.5B's 32K context with headroom for the response)."""
    body = body[:max_chars]
    return (
        f"<|im_start|>system\n{_GENERIC_SYSTEM}<|im_end|>\n"
        f"<|im_start|>user\n{body}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


# ─── Tiny-doc shortcut ──────────────────────────────────────────────────────

def tiny_doc_summary(body: str) -> SummaryResult:
    """For docs under ~200 chars (a one-line thought, a short voice note),
    the document IS its own summary. Skip the LLM call."""
    summary = body.strip().split("\n", 1)[0][:280] or "(empty)"
    return SummaryResult(
        summary=summary, topics=[],
        raw=summary,
        model_version="tiny-doc-shortcut@1",
        profile_version=_PROFILE_VERSION,
    )


# ─── Llama.cpp summarizer ──────────────────────────────────────────────────

class LlamaSummarizer:
    """Single-process, lazy-loaded llama.cpp summarizer.

    Model selection priority:
      1. Explicit `artefact` arg (constructed with a ModelArtefact)
      2. `model_name` arg (resolved via runtime catalog)
      3. DEFAULT_SUMMARY_MODEL_NAME (currently Qwen2.5-1.5B Q4_K_M)
    """

    def __init__(
        self,
        *,
        artefact:   ModelArtefact | None = None,
        model_name: str | None = None,
        hw:         HardwareInfo | None = None,
        n_ctx:      int = 16384,                  # well within Qwen-1.5B's 32K
    ) -> None:
        if artefact is not None:
            self._artefact = artefact
        else:
            self._artefact = resolve_model(model_name or DEFAULT_SUMMARY_MODEL_NAME)
        self._hw = hw or probe()
        self._n_ctx = n_ctx
        self._llm: Llama | None = None
        self._lock = asyncio.Lock()

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
            path = fetch(self._artefact)
            n_gpu_layers = -1 if self._hw.backend in {Backend.METAL, Backend.CUDA, Backend.ROCM} else 0
            log.info("summarizer.load_begin",
                     model=self.model_version, n_gpu_layers=n_gpu_layers)

            # IMPORTANT: verbose=True. Why:
            #
            # llama-cpp-python with verbose=False opens a `suppress_stdout_stderr`
            # context at __init__ AND keeps it on its ExitStack for the lifetime
            # of the Llama instance. That context dup2's /dev/null over fd 1/2
            # AND reassigns sys.stdout/sys.stderr to /dev/null TextIOWrappers —
            # process-wide. Every concurrent asyncio task in this process
            # silently loses its prints/logs until the Llama is destroyed.
            #
            # We hit this in v0.1: chunk stage runs in parallel with summarize
            # (both subscribe to doc.normalized). Chunk's `log.info("chunk.ok")`
            # fired during the ~3s window when Llama was loading — went straight
            # to /dev/null, never appeared in worker logs even though the data
            # committed correctly.
            #
            # verbose=True keeps sys.stdout/sys.stderr pristine. The cost is a
            # handful of llama.cpp information lines (model metadata, BOS/EOS
            # tokens, prompt/eval timings) mixed into stderr during load and per
            # generation. Acceptable for v0.1; cleaner separation (LLM in a
            # subprocess) is a v0.2 task.
            self._llm = await asyncio.to_thread(
                lambda: Llama(
                    model_path=str(path),
                    n_ctx=self._n_ctx,
                    n_threads=self._hw.threads,
                    n_gpu_layers=n_gpu_layers,
                    logits_all=False,
                    verbose=True,
                    seed=0,
                )
            )
            log.info("summarizer.loaded", model=self.model_version)

    async def summarize(self, body: str, *, max_tokens: int = 256) -> SummaryResult:
        # Tiny-doc shortcut: skip LLM call for very short bodies.
        if len(body) < 200:
            return tiny_doc_summary(body)

        await self._ensure_loaded()
        prompt = build_prompt(body)

        def _run() -> str:
            assert self._llm is not None
            # Non-streaming call → a completion response dict; llama-cpp types
            # it as a response|iterator union, so treat as Any and index.
            out: Any = self._llm(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=0.0,
                top_p=1.0,
                stop=["<|im_end|>"],
            )
            return str(out["choices"][0]["text"])

        raw = await asyncio.to_thread(_run)
        return self._parse(raw, model_version=self.model_version)

    @staticmethod
    def _parse(raw: str, *, model_version: str) -> SummaryResult:
        text = raw.strip()
        # Strip a leading code-fence the model might emit despite the system prompt.
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").lstrip()
        # Find the first { ... } block.
        start = text.find("{")
        end   = text.rfind("}")
        summary, topics = "", []
        if 0 <= start < end:
            try:
                obj = json.loads(text[start:end+1])
                summary = (obj.get("summary") or "").strip()
                topics  = [str(t) for t in (obj.get("topics") or []) if str(t).strip()][:5]
            except json.JSONDecodeError:
                pass
        if not summary:
            # Fall back to first non-empty line — never crash the pipeline on a bad parse.
            for line in text.splitlines():
                if line.strip():
                    summary = line.strip()[:280]
                    break
        return SummaryResult(
            summary=summary or "(no summary produced)",
            topics=topics,
            raw=raw,
            # Reported version is whatever artefact actually loaded — passed
            # in by .summarize() so there's no env var or label drift.
            model_version=model_version,
            profile_version=_PROFILE_VERSION,
        )
