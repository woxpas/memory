"""woxpas-unlock — sovereign boot daemon.

Flow:
  1. Connect to Postgres, read the tenant's wrapped DEK + KDF params.
  2. Prompt for the passphrase on the controlling TTY.
  3. Verify passphrase against the stored Argon2id verifier (fast check).
  4. Derive KEK via Argon2id KDF, unwrap DEK (AES-256-GCM).
  5. Open a Unix socket at ~/.woxpas/dek.sock (chmod 600).
  6. Serve DEK bytes on request. Zeroize on idle timeout or explicit lock.

The daemon holds exactly one tenant's DEK at a time (v0.1 — single-tenant).
Multi-tenant installs run one daemon per tenant on separate sockets.

Protocol (line-delimited JSON with EOF to signal end-of-request):
  → {"action": "unlock", "tenant_id": "<uuid>"}
  ← {"status": "ok", "dek_hex": "<64-char hex>"}
  ← {"status": "error", "message": "..."}

  → {"action": "health"}
  ← {"status": "ok", "uptime": 123.4, "tenant_id": "<uuid>"}

  → {"action": "lock"}
  ← {"status": "locked"}
  (daemon exits after responding)

Usage:
  woxpas-unlock                          # uses WOXPAS_DEV_TENANT_ID_DEFAULT
  woxpas-unlock --tenant-id <uuid>       # explicit tenant
  woxpas-unlock --idle-lock-sec 1800     # lock after 30 min idle (default 900)
  woxpas-unlock --socket ~/.woxpas/dek.sock
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from woxpas_storage import PostgresPool, StorageSettings
from woxpas_storage.envelope import (
    WrappedDek,
    derive_kek_from_params,
    unwrap_dek,
    verify_passphrase,
)

log = structlog.get_logger(__name__)

DEFAULT_SOCKET_PATH = os.path.expanduser("~/.woxpas/dek.sock")
DEFAULT_IDLE_LOCK_SEC = 900  # 15 minutes
SOCKET_BACKLOG = 1  # only a few local processes connect


# ─── socket helpers ──────────────────────────────────────────────────────────

def _ensure_socket_dir(socket_path: str) -> None:
    """Create the parent directory with mode 0700 if it doesn't exist."""
    d = os.path.dirname(socket_path)
    Path(d).mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)


def _remove_stale_socket(socket_path: str) -> None:
    """Remove a leftover socket file from a previous run."""
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass


# ─── request handler ─────────────────────────────────────────────────────────

async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    tenant_id: UUID,
    dek_hex: str,
    start_time: float,
    state: _DaemonState,
) -> None:
    """Handle a single client connection — one request, one response, close."""
    try:
        raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    except TimeoutError:
        writer.close()
        return

    try:
        request = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _respond(writer, {"status": "error", "message": "invalid JSON"})
        return

    action = request.get("action", "")

    if action == "unlock":
        req_tenant = request.get("tenant_id", "")
        if str(tenant_id) != req_tenant:
            _respond(writer, {
                "status": "error",
                "message": f"tenant mismatch: daemon holds {tenant_id}, requested {req_tenant}",
            })
        else:
            _respond(writer, {
                "status": "ok",
                "dek_hex": dek_hex,
                "tenant_id": str(tenant_id),
            })

    elif action == "health":
        _respond(writer, {
            "status": "ok",
            "alive": True,
            "uptime": round(time.monotonic() - start_time, 1),
            "tenant_id": str(tenant_id),
        })

    elif action == "lock":
        _respond(writer, {"status": "locked"})
        state.lock_requested = True

    else:
        _respond(writer, {"status": "error", "message": f"unknown action: {action}"})


def _respond(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """Send a JSON response and close the write side."""
    try:
        writer.write(json.dumps(payload).encode("utf-8"))
        writer.write_eof()
    except OSError:
        pass
    writer.close()


# ─── server ──────────────────────────────────────────────────────────────────

class _DaemonState:
    """Mutable state shared between the server loop and signal handlers."""
    def __init__(self) -> None:
        self.lock_requested = False
        self.idle_event = asyncio.Event()


async def _run_server(
    socket_path: str,
    tenant_id: UUID,
    dek_hex: str,
    idle_lock_sec: int,
    state: _DaemonState,
) -> None:
    """Open the Unix socket and serve until lock or shutdown."""
    start_time = time.monotonic()
    _ensure_socket_dir(socket_path)
    _remove_stale_socket(socket_path)

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(
            r, w, tenant_id=tenant_id, dek_hex=dek_hex, start_time=start_time,
            state=state,
        ),
        path=socket_path,
        backlog=SOCKET_BACKLOG,
    )
    os.chmod(socket_path, 0o600)

    log.info("daemon.listening", socket_path=socket_path, tenant_id=str(tenant_id))

    # Track last activity for idle timeout.
    # We use a simple approach: after each connection, reset a timer.
    # The server runs with serve_forever() but we poll for the idle timeout
    # and lock signal by periodically checking.
    last_activity = time.monotonic()

    # We can't easily hook into serve_forever's connection events, so we
    # run a polling loop with a short sleep.
    idle_task = asyncio.create_task(_idle_watchdog(
        idle_lock_sec, state, last_activity,
    ))

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass

    server.close()
    await server.wait_closed()
    _remove_stale_socket(socket_path)
    log.info("daemon.socket_closed", socket_path=socket_path)


async def _idle_watchdog(
    idle_lock_sec: int,
    state: _DaemonState,
    last_activity: float,
) -> None:
    """Periodically check for idle timeout or lock request."""
    check_interval = min(idle_lock_sec // 4, 60)  # check at least every 60s
    if check_interval < 1:
        check_interval = 1

    while True:
        await asyncio.sleep(check_interval)
        if state.lock_requested:
            log.info("daemon.lock_requested")
            # Stop the event loop (which cancels serve_forever)
            asyncio.get_event_loop().stop()
            return

        elapsed = time.monotonic() - last_activity
        if elapsed >= idle_lock_sec:
            log.info("daemon.idle_timeout", elapsed_sec=round(elapsed, 1))
            asyncio.get_event_loop().stop()
            return


# ─── unlock flow ─────────────────────────────────────────────────────────────

async def _unlock_tenant(
    pool: PostgresPool,
    tenant_id: UUID,
) -> tuple[bytes, str]:
    """Read the wrapped envelope from Postgres, prompt for passphrase, unwrap DEK.

    Returns (dek_bytes, dek_hex).
    Raises SystemExit if the passphrase is wrong or the tenant is locked.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT passphrase_verifier, kdf_params,
                   encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version,
                   status, failed_unlock_count, locked_until
            FROM tenants
            WHERE tenant_id = $1
            """,
            tenant_id,
        )

    if row is None:
        raise SystemExit(f"tenant not found: {tenant_id}")

    if row["status"] == "disabled":
        raise SystemExit(f"tenant {tenant_id} is disabled")

    if row["status"] == "locked" and row["locked_until"] is not None:
        if row["locked_until"] > time.time():
            raise SystemExit(
                f"tenant {tenant_id} is locked until {row['locked_until']}"
            )

    verifier: bytes = row["passphrase_verifier"]
    kdf_params: dict[str, Any] = json.loads(row["kdf_params"]) if isinstance(
        row["kdf_params"], str
    ) else row["kdf_params"]

    # Prompt for passphrase.
    passphrase = _prompt_passphrase()
    if not passphrase:
        raise SystemExit("passphrase required")

    # Step 1 — fast passphrase check via Argon2id verifier.
    print("Verifying passphrase…", file=sys.stderr)
    if not verify_passphrase(verifier, passphrase):
        print("ERROR: wrong passphrase", file=sys.stderr)
        # Increment failed_unlock_count asynchronously (best-effort).
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE tenants
                    SET failed_unlock_count = failed_unlock_count + 1
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                )
        except Exception:
            log.debug("daemon.failed_unlock_count_update_failed", exc_info=True)
        raise SystemExit(1)

    # Step 2 — derive KEK (expensive, intentional).
    print("Deriving KEK (Argon2id — this takes a moment)…", file=sys.stderr)
    try:
        kek = derive_kek_from_params(passphrase, kdf_params)
    except Exception as e:
        raise SystemExit(f"KEK derivation failed: {e}") from e

    # Step 3 — build WrappedDek from DB columns and unwrap.
    wrapped = WrappedDek(
        encrypted_dek=row["encrypted_dek"],
        iv=row["encrypted_dek_iv"],
        tag=row["encrypted_dek_tag"],
        kdf_params=kdf_params,
        dek_version=row["dek_version"],
    )

    try:
        dek = unwrap_dek(wrapped, kek)
    except ValueError as e:
        raise SystemExit(f"DEK unwrap failed: {e}") from e

    # Zeroize the KEK — we no longer need it.
    kek = b"\x00" * len(kek)

    # Reset failed_unlock_count on successful unlock.
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tenants
                SET failed_unlock_count = 0, locked_until = NULL, status = 'active'
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
    except Exception:
        log.debug("daemon.reset_failed_count_failed", exc_info=True)

    print(f"Unlocked tenant {tenant_id}", file=sys.stderr)
    return dek, dek.hex()


def _prompt_passphrase() -> str:
    """Read passphrase from TTY. Respects WOXPAS_UNLOCK_PASSPHRASE for non-interactive use."""
    env_pass = os.environ.get("WOXPAS_UNLOCK_PASSPHRASE")
    if env_pass:
        return env_pass
    try:
        return getpass.getpass("Passphrase: ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        raise SystemExit(1) from None


# ─── entry point ─────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="woxpas-unlock",
        description="Woxpas boot daemon — passphrase → KEK → DEK over Unix socket",
    )
    p.add_argument(
        "--tenant-id", default=None,
        help="Tenant UUID (default: $WOXPAS_DEV_TENANT_ID_DEFAULT)",
    )
    p.add_argument(
        "--socket", default=DEFAULT_SOCKET_PATH,
        help=f"Unix socket path (default: {DEFAULT_SOCKET_PATH})",
    )
    p.add_argument(
        "--idle-lock-sec", type=int, default=DEFAULT_IDLE_LOCK_SEC,
        help=f"Seconds of inactivity before auto-lock (default: {DEFAULT_IDLE_LOCK_SEC})",
    )
    p.add_argument(
        "--foreground", action="store_true",
        help="Stay in foreground (default: daemonize after unlock)",
    )
    return p.parse_args(argv)


async def _async_main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Resolve tenant.
    tenant_id_str = args.tenant_id or os.environ.get("WOXPAS_DEV_TENANT_ID_DEFAULT")
    if not tenant_id_str:
        print(
            "ERROR: --tenant-id is required (or set WOXPAS_DEV_TENANT_ID_DEFAULT)",
            file=sys.stderr,
        )
        return 1
    tenant_id = UUID(tenant_id_str)

    # Connect to Postgres.
    settings = StorageSettings()
    pool = await PostgresPool.from_settings(settings.postgres)
    try:
        dek, dek_hex = await _unlock_tenant(pool, tenant_id)
    finally:
        await pool.close()

    # Daemon loop.
    state = _DaemonState()

    # Install signal handlers for clean shutdown.
    loop = asyncio.get_event_loop()

    def _on_signal() -> None:
        log.info("daemon.signal_received")
        state.lock_requested = True
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows — no signal handlers on ProactorEventLoop.
            pass

    await _run_server(
        socket_path=args.socket,
        tenant_id=tenant_id,
        dek_hex=dek_hex,
        idle_lock_sec=args.idle_lock_sec,
        state=state,
    )

    # Zeroize the DEK before exiting.
    # bytes are immutable in Python; best we can do is overwrite the local refs.
    dek = b"\x00" * len(dek)
    dek_hex = "0" * len(dek_hex)
    log.info("daemon.shutdown_complete")
    return 0


def main(argv: list[str] | None = None) -> None:
    """Entry point registered as `woxpas-unlock`."""
    rc = asyncio.run(_async_main(argv))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
