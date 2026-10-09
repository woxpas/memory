"""Lightweight client that fetches a DEK from the unlock daemon over a Unix socket.

Imported by PipelineContext.construct() as the first DEK source — if the
daemon socket exists and responds, the DEK comes from the unlocked daemon
rather than the WOXPAS_DEV_DEK_HEX env var. If the socket doesn't exist,
falls through to DevDekSettings silently (dev convenience).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

DEK_SOCKET_PATH = os.path.expanduser("~/.woxpas/dek.sock")
_SOCKET_TIMEOUT = 5.0  # seconds — fail fast if daemon is stuck


class BootClientError(Exception):
    """Raised when the daemon refuses or a protocol error occurs."""


async def fetch_dek(
    tenant_id: str,
    *,
    socket_path: str = DEK_SOCKET_PATH,
    timeout_sec: float = _SOCKET_TIMEOUT,
) -> bytes:
    """Connect to the unlock daemon and request the DEK for *tenant_id*.

    Returns the raw DEK bytes on success.
    Raises BootClientError on any failure (wrong passphrase, daemon down, etc).
    """
    if not os.path.exists(socket_path):  # noqa: ASYNC240
        raise BootClientError(f"daemon socket not found: {socket_path}")

    request = json.dumps({"action": "unlock", "tenant_id": tenant_id})

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=timeout_sec,
        )
    except (ConnectionRefusedError, FileNotFoundError) as e:
        raise BootClientError(f"daemon socket unreachable: {socket_path}") from e
    except TimeoutError:
        raise BootClientError(
            f"daemon socket connect timed out after {timeout_sec}s"
        ) from None

    try:
        writer.write(request.encode("utf-8"))
        writer.write_eof()
        await writer.drain()

        raw = await asyncio.wait_for(reader.read(), timeout=timeout_sec)
        writer.close()
        await writer.wait_closed()
    except TimeoutError:
        raise BootClientError(
            f"daemon did not respond within {timeout_sec}s"
        ) from None
    except OSError as e:
        raise BootClientError(f"daemon socket I/O error: {e}") from e

    try:
        response: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BootClientError(f"daemon returned invalid JSON: {e}") from e

    if response.get("status") != "ok":
        msg = response.get("message", "unknown error")
        raise BootClientError(f"daemon refused: {msg}")

    dek_hex = response.get("dek_hex")
    if not dek_hex or not isinstance(dek_hex, str):
        raise BootClientError("daemon response missing dek_hex")

    try:
        return bytes.fromhex(dek_hex)
    except ValueError as e:
        raise BootClientError(f"daemon returned invalid hex DEK: {e}") from e


async def daemon_health(
    *,
    socket_path: str = DEK_SOCKET_PATH,
    timeout_sec: float = _SOCKET_TIMEOUT,
) -> dict[str, Any]:
    """Check whether the daemon is alive and unlocked."""
    if not os.path.exists(socket_path):  # noqa: ASYNC240
        return {"alive": False, "reason": "socket not found"}

    request = json.dumps({"action": "health"})
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=timeout_sec,
        )
        writer.write(request.encode("utf-8"))
        writer.write_eof()
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=timeout_sec)
        writer.close()
        await writer.wait_closed()
        result: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return result
    except Exception as e:
        return {"alive": False, "reason": str(e)}


async def send_lock(
    *,
    socket_path: str = DEK_SOCKET_PATH,
    timeout_sec: float = _SOCKET_TIMEOUT,
) -> dict[str, Any]:
    """Tell the daemon to lock immediately (zeroize DEK, close socket)."""
    if not os.path.exists(socket_path):  # noqa: ASYNC240
        return {"status": "not_running"}

    request = json.dumps({"action": "lock"})
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=timeout_sec,
        )
        writer.write(request.encode("utf-8"))
        writer.write_eof()
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=timeout_sec)
        writer.close()
        await writer.wait_closed()
        result: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}
