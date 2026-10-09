"""Content-derived UUIDs.

block_id, chunk_id, span_id are *not* random — they are SHA-256 of normalized
content. Re-ingesting the same input converges on the same IDs, which is what
keeps span-level UI bookmarks and entity-mention rows valid across pipeline
upgrades. ContentHashable defines the contract every hashable type implements.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Protocol, runtime_checkable

# Stable namespace for woxpas content ids (RFC 4122 v5 uses SHA-1; we use a
# prefix-namespaced SHA-256 + UUID v8-style packing for full-bit hashes).
_NS_WOXPAS = b"woxpas:v1:"


def content_uuid(*parts: str | bytes | int) -> uuid.UUID:
    """Deterministic UUID from content.

    Pack parts into a stable byte string, hash with SHA-256, take first 16 bytes
    as the UUID. v8 layout (custom). Same inputs → same UUID, always.
    """
    h = hashlib.sha256()
    h.update(_NS_WOXPAS)
    for p in parts:
        if isinstance(p, str):
            h.update(p.encode("utf-8"))
        elif isinstance(p, bytes):
            h.update(p)
        else:
            h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")  # unit separator
    digest = h.digest()[:16]
    # Mark as version 8 (custom), variant RFC 4122
    b = bytearray(digest)
    b[6] = (b[6] & 0x0F) | 0x80          # version 8
    b[8] = (b[8] & 0x3F) | 0x80          # variant
    return uuid.UUID(bytes=bytes(b))


@runtime_checkable
class ContentHashable(Protocol):
    """Anything that produces a stable byte representation for hashing."""
    def content_signature(self) -> bytes: ...
