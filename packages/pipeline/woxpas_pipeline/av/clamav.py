"""Async ClamAV INSTREAM client.

ClamAV's INSTREAM command takes a stream of length-prefixed chunks (4-byte BE
size + chunk bytes), terminated by a zero-length chunk. clamd replies with
either "stream: OK\\0" or "stream: <SIG> FOUND\\0".

We open one connection per scan (clamd handles concurrency via its own thread
pool); for hot pipelines we run multiple scans concurrently bounded by an
asyncio.Semaphore in the AV stage.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from enum import StrEnum

import structlog

from woxpas_pipeline.settings import ClamAVSettings

log = structlog.get_logger(__name__)


class ScanResult(StrEnum):
    CLEAN    = "clean"
    INFECTED = "infected"
    ERROR    = "error"


@dataclass(slots=True)
class ScanReport:
    result: ScanResult
    signature: str | None = None
    raw_response: str | None = None


# clamd default: chunks must be ≤ StreamMaxLength (default 25M); we pick
# something pragmatic for low memory pressure.
_CHUNK_SIZE = 64 * 1024


class ClamAVClient:
    def __init__(self, settings: ClamAVSettings) -> None:
        self._settings = settings
        if not settings.enabled:
            # Pillar 3 (security): AV scanning is a hard part of the ingestion
            # contract. Disabling it means every uploaded file is treated as
            # clean without inspection — never do this silently. Warn loudly
            # so an operator can't turn it off by accident and forget.
            log.warning(
                "clamav.disabled",
                message="ClamAV scanning is DISABLED — all ingested files "
                        "will be accepted without virus scanning. This is "
                        "unsafe for any untrusted input.",
            )

    async def ping(self) -> bool:
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(self._settings.host, self._settings.port),
                timeout=self._settings.timeout_s,
            )
            try:
                w.write(b"zPING\x00")
                await w.drain()
                resp = await asyncio.wait_for(r.readuntil(b"\x00"), 5.0)
                return resp.startswith(b"PONG")
            finally:
                w.close()
                await w.wait_closed()
        except Exception as e:
            log.warning("clamav.ping.failed", error=str(e))
            return False

    async def scan_bytes(self, content: bytes) -> ScanReport:
        if not self._settings.enabled:
            return ScanReport(result=ScanResult.CLEAN, raw_response="disabled")

        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(self._settings.host, self._settings.port),
                timeout=self._settings.timeout_s,
            )
        except (TimeoutError, OSError) as e:
            log.error("clamav.connect.failed", error=str(e))
            return ScanReport(result=ScanResult.ERROR, raw_response=f"connect: {e}")

        try:
            # zINSTREAM uses NUL-terminated framing (vs nINSTREAM newline-term).
            w.write(b"zINSTREAM\x00")
            await w.drain()

            for offset in range(0, len(content), _CHUNK_SIZE):
                chunk = content[offset:offset + _CHUNK_SIZE]
                w.write(struct.pack("!I", len(chunk)) + chunk)
            # Terminator: zero-length chunk
            w.write(struct.pack("!I", 0))
            await w.drain()

            try:
                resp_raw = await asyncio.wait_for(
                    r.readuntil(b"\x00"), self._settings.timeout_s,
                )
            except TimeoutError:
                return ScanReport(result=ScanResult.ERROR, raw_response="timeout")
            resp = resp_raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
        finally:
            try:
                w.close()
                await w.wait_closed()
            except Exception:  # noqa: S110  # best-effort close of the scan socket; the scan result is already read
                pass

        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp: str) -> ScanReport:
        # Examples:
        #   "stream: OK"
        #   "stream: Win.Test.EICAR_HDB-1 FOUND"
        #   "stream: ERROR: <message>"
        if resp.endswith(" OK"):
            return ScanReport(result=ScanResult.CLEAN, raw_response=resp)
        if resp.endswith(" FOUND"):
            try:
                _, sig_token = resp.split(": ", 1)
                sig = sig_token.removesuffix(" FOUND").strip()
            except ValueError:
                sig = None
            return ScanReport(result=ScanResult.INFECTED, signature=sig, raw_response=resp)
        return ScanReport(result=ScanResult.ERROR, raw_response=resp)
