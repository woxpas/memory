"""woxpas-boot — sovereign boot daemon.

The unlock daemon prompts for a passphrase, derives the KEK via Argon2id,
unwraps the tenant DEK from the Postgres-persisted envelope, and serves it
to the worker / API / MCP server over a local Unix socket.

This closes the plaintext-DEK-in-.env gap. OS full-disk encryption
(FileVault / LUKS) covers the datastore files at rest.
"""

from woxpas_boot.client import (
    DEK_SOCKET_PATH,
    BootClientError,
    daemon_health,
    fetch_dek,
    send_lock,
)

__all__ = [
    "DEK_SOCKET_PATH",
    "BootClientError",
    "daemon_health",
    "fetch_dek",
    "send_lock",
]
