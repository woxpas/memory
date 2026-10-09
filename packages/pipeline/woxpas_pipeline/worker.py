"""Pipeline worker entry point.

Long-running process. Constructs the PipelineContext, registers stage handlers
on the OutboxPublisher, and runs until SIGTERM/SIGINT.

Usage:
  python -m woxpas_pipeline.worker
  woxpas-worker  (entry point declared in pyproject)
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Protocol

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import StorageSettings

from woxpas_pipeline.av import AvStage
from woxpas_pipeline.chunk import ChunkStage
from woxpas_pipeline.conflict_detect import ConflictDetectionStage
from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.detect import DetectStage
from woxpas_pipeline.embed import EmbedIndexStage
from woxpas_pipeline.extract import ExtractStage
from woxpas_pipeline.extract_facts import ExtractFactsStage
from woxpas_pipeline.link import LinkingStage
from woxpas_pipeline.sensitivity import SensitivityRefineStage
from woxpas_pipeline.settings import (
    ClamAVSettings,
    DevDekSettings,
    PipelineSettings,
)
from woxpas_pipeline.summarize import SummarizeStage
from woxpas_pipeline.supersession import SupersessionStage

log = structlog.get_logger(__name__)


class StageHandler(Protocol):
    name: str
    def event_type(self) -> EventType: ...
    async def handle(self, event: Event) -> None: ...


def _configure_logging() -> None:
    # Line-buffer stdout so concurrent log emissions from multiple stages running
    # under asyncio.gather flush promptly when the output is piped (tee, docker logs).
    # Without this, block-buffering can stall log lines for seconds at a time.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


async def run() -> int:
    _configure_logging()
    log.info("worker.starting")

    ctx = await PipelineContext.construct(
        storage=StorageSettings(),
        pipeline=PipelineSettings(),
        clamav=ClamAVSettings(),
        dev=DevDekSettings(),
    )

    # Bootstrap downstream stores. Idempotent on repeat starts.
    await ctx.qdrant.bootstrap()
    await ctx.objstore.bootstrap()

    # Pre-warm the embedder so the first ingested document doesn't pay the
    # ONNX model load cost (~3s). A single warmup embed resolves the artefact,
    # opens the ONNX session, loads the tokenizer, and primes the inference
    # thread. The result is discarded — the session stays resident.
    if ctx.enable_runtime:
        try:
            print("  warming embedder…", end=" ", flush=True)
            embedder = await ctx.embedder()
            await embedder.embed(["warmup"], batch_size=1)
            print("✓")
            log.info("worker.embedder_warmed", model=embedder.model_version)
        except Exception:
            print("✗")
            log.warning("worker.embedder_warmup_failed", exc_info=True)

    # Register stage handlers.
    stages: list[StageHandler] = [
        AvStage(ctx),
        DetectStage(ctx),
        ExtractStage(ctx),
        SummarizeStage(ctx),
        SensitivityRefineStage(ctx),
        ChunkStage(ctx),
        EmbedIndexStage(ctx),
        SupersessionStage(ctx),
        ExtractFactsStage(ctx),
        LinkingStage(ctx),
        ConflictDetectionStage(ctx),
    ]
    for stage in stages:
        evt = stage.event_type().value
        ctx.publisher.subscribe(evt, stage.handle)
        log.info("worker.stage_registered", stage=stage.name, event_type=evt)

    await ctx.publisher.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows-on-WSL or unsupported envs — fall back to default handlers.
            pass

    log.info("worker.ready")
    try:
        await stop_event.wait()
    finally:
        log.info("worker.stopping")
        await ctx.close()
        log.info("worker.stopped")
    return 0


def main() -> None:
    """Synchronous entry point for the pyproject `woxpas-worker` script."""
    from woxpas_pipeline.boot import load_env
    load_env()
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
