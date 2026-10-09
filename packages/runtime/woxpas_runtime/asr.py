"""Whisper ASR via faster-whisper (CTranslate2-based, no torch).

Single-process, lazy-loaded. Same shape as the embedder/summarizer:
  - constructed once per worker
  - lazy-load on first transcribe() call
  - device picked from hwprobe (CPU on Intel Mac, CUDA on NVIDIA, etc.)
  - settings-driven model name with catalog default

Output: list of Segments with {start_ms, end_ms, text, segment_id}. The audio
extractor wraps these into BlockType.AUDIO_SEGMENT blocks with AudioLocator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

from woxpas_runtime.hwprobe import Backend, HardwareInfo, probe
from woxpas_runtime.models import (
    DEFAULT_ASR_MODEL_NAME,
    ModelArtefact,
    fetch,
    resolve_model,
)

log = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class Segment:
    segment_id: str
    t_start_ms: int
    t_end_ms:   int
    text:       str


@dataclass(slots=True)
class TranscriptionResult:
    segments:   list[Segment]
    language:   str | None
    duration_ms: int
    model_version: str


class WhisperAsr:
    """faster-whisper ASR. Lazy-loaded on first call."""

    def __init__(
        self,
        *,
        artefact:   ModelArtefact | None = None,
        model_name: str | None = None,
        hw:         HardwareInfo | None = None,
    ) -> None:
        if artefact is not None:
            self._artefact = artefact
        else:
            self._artefact = resolve_model(model_name or DEFAULT_ASR_MODEL_NAME)
        self._hw = hw or probe()
        self._model: WhisperModel | None = None
        self._lock = asyncio.Lock()

    @property
    def model_version(self) -> str:
        return self._artefact.model_version

    async def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        async with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel
            path = await asyncio.to_thread(fetch, self._artefact)

            # CT2 device strings differ from torch:
            #   "cuda" / "cuda:0" → CUDA
            #   "auto"            → CT2 picks (usually CPU on macOS Intel)
            #   "cpu"             → force CPU
            device = "cuda" if self._hw.backend in {Backend.CUDA, Backend.ROCM} else "cpu"
            compute_type = "int8" if device == "cpu" else "float16"

            log.info("asr.load_begin",
                     model=self.model_version, device=device, compute_type=compute_type,
                     path=str(path))
            self._model = await asyncio.to_thread(
                lambda: WhisperModel(
                    str(path),
                    device=device, compute_type=compute_type,
                    cpu_threads=self._hw.threads,
                )
            )
            log.info("asr.loaded", model=self.model_version)

    async def transcribe(
        self, audio_path: str, *,
        language: str | None = None,
        beam_size: int = 1,        # 1 = greedy; faster, slightly less accurate
    ) -> TranscriptionResult:
        await self._ensure_loaded()
        assert self._model is not None

        def _run() -> TranscriptionResult:
            assert self._model is not None
            segments_iter, info = self._model.transcribe(
                audio_path,
                language=language,
                beam_size=beam_size,
                vad_filter=True,        # cuts long silences; saves work + cleaner output
                vad_parameters={"min_silence_duration_ms": 500},
            )
            segs: list[Segment] = []
            for s in segments_iter:
                segs.append(Segment(
                    segment_id=str(s.id),
                    t_start_ms=int(s.start * 1000),
                    t_end_ms=int(s.end * 1000),
                    text=s.text.strip(),
                ))
            duration_ms = int(info.duration * 1000) if info.duration else 0
            return TranscriptionResult(
                segments=segs,
                language=info.language,
                duration_ms=duration_ms,
                model_version=self.model_version,
            )

        return await asyncio.to_thread(_run)
