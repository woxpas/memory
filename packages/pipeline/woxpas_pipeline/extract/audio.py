"""Audio (voice) extractor — runs Whisper ASR on the raw bytes and produces
one audio_segment block per Whisper segment.

Unlike text/PDF/DOCX extractors which run as pure functions, this one needs
the runtime ASR model. So the extractor's `extract()` method is async and
takes a context. The Extract stage routes audio MIMEs to this class via
`extract_async()` instead of `extract()`.

The CanonicalDoc here has:
  - blocks: one BlockType.AUDIO_SEGMENT per Whisper segment
  - source_locator on each: AudioLocator { segment_id, t_start_ms, t_end_ms, speaker? }
  - doc_metadata.duration_ms set from Whisper's duration estimate
  - doc_metadata.language set from Whisper's language detection

Speaker diarization is NOT in v0.1; speaker=None. Adding it later is additive.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from typing import Any

from woxpas_core import (
    AudioLocator,
    Block,
    BlockType,
    CanonicalDoc,
    DocMetadata,
    StructureIndex,
)

from woxpas_pipeline.extract.base import (
    ExtractInput,
    ExtractMetrics,
    Extractor,
    ExtractResult,
)


class AudioExtractor(Extractor):
    """Audio → CanonicalDoc via Whisper ASR.

    Note: this extractor is special — it requires runtime model access via the
    PipelineContext. The Extract stage detects this class and calls
    `extract_async(inp, ctx)` instead of the protocol's `extract(inp)`.
    """
    name = "audio"
    extractor_version = "0.1.0"
    supported_mimes = (
        "audio/wav", "audio/x-wav", "audio/wave",
        "audio/mpeg", "audio/mp3",
        "audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac",
        "audio/ogg", "audio/vorbis",
        "audio/flac", "audio/x-flac",
        "audio/webm",
    )

    # ─── Synchronous protocol method (raises; the audio path is async-only) ──
    def extract(self, inp: ExtractInput) -> ExtractResult:
        raise RuntimeError(
            "AudioExtractor.extract() is async-only; call extract_async() instead. "
            "The Extract stage handles this routing."
        )

    # ─── Async path used by the Extract stage ────────────────────────────────
    async def extract_async(self, inp: ExtractInput, ctx: Any) -> ExtractResult:
        # Whisper takes a file path. Write the bytes to a temp file under the
        # OS tmpfs (or a worker tmpfs in containers). Cleaned up after transcribe.
        suffix = _suffix_for_mime(inp.mime, inp.filename)
        with tempfile.NamedTemporaryFile(prefix="woxpas-audio-", suffix=suffix, delete=False) as tmp:
            tmp.write(inp.content)
            tmp_path = tmp.name
        try:
            asr = await ctx.asr()
            transcription = await asr.transcribe(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        blocks: list[Block] = []
        full_text_chars = 0
        for seg in transcription.segments:
            if not seg.text:
                continue
            full_text_chars += len(seg.text)
            locator = AudioLocator(
                segment_id=seg.segment_id,
                t_start_ms=seg.t_start_ms,
                t_end_ms=seg.t_end_ms,
            )
            blocks.append(Block.make(
                doc_id=inp.doc_id,
                ordinal=len(blocks),
                type_=BlockType.AUDIO_SEGMENT,
                text=seg.text,
                source_locator=locator,
                attrs={"asr_segment_id": seg.segment_id},
            ))

        cdoc = CanonicalDoc(
            doc_id=inp.doc_id,
            tenant_id=inp.tenant_id,
            pipeline_version=inp.pipeline_version,
            blocks=blocks,
            doc_metadata=DocMetadata(
                title=inp.declared_title,
                language=transcription.language or inp.declared_language,
                duration_ms=transcription.duration_ms,
                source_format=inp.mime,
                extra={"asr_model_version": transcription.model_version},
            ),
            structure_index=StructureIndex(),
            extractor=self.name,
            extractor_version=self.extractor_version,
            extracted_at=datetime.now(UTC),
        )
        return ExtractResult(
            canonical_doc=cdoc,
            metrics=ExtractMetrics(
                blocks=len(blocks),
                chars=full_text_chars,
                extra={
                    "duration_ms": transcription.duration_ms,
                    "asr_model_version": transcription.model_version,
                    "language": transcription.language,
                },
            ),
        )


def _suffix_for_mime(mime: str, filename: str | None) -> str:
    """Best-effort file suffix so libsndfile/ffmpeg downstream of CT2 can sniff format."""
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1]
    table = {
        "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
        "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac",
        "audio/ogg": ".ogg", "audio/vorbis": ".ogg",
        "audio/flac": ".flac", "audio/x-flac": ".flac",
        "audio/webm": ".webm",
    }
    return table.get(mime.split(";", 1)[0].strip(), ".bin")
