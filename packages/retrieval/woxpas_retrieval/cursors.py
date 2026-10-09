"""Opaque pagination cursors.

Server encodes whatever state it needs (last id + score, or last timestamp,
or page offset). Client treats the string as a black box. This isolates the
pagination strategy from the API contract — we can switch from offset to
keyset to seek-paginated cursors without breaking clients.

For v1, cursors carry:
  - `last_id`           — primary key of the last item returned (for keyset)
  - `last_sort_value`   — the value of the column the query orders by
                          (timestamp / score / etc.)
  - `version`           — schema version of the cursor (so we can evolve)

Encoding: JSON → URL-safe base64 (no padding). Trivially decodable but the
client SHOULD NOT inspect — that's a future-fragility trap.
"""

from __future__ import annotations

import base64
import json
from typing import Any

_CURSOR_VERSION = "1"


def encode_cursor(state: dict[str, Any]) -> str:
    """Build an opaque cursor string from server-side state."""
    payload = {"v": _CURSOR_VERSION, **state}
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """Reverse encode_cursor. Returns None if the cursor is missing or
    malformed — caller falls back to an unpaginated query (page 1)."""
    if not cursor:
        return None
    # Re-pad for base64 decoding.
    pad = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode((cursor + pad).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != _CURSOR_VERSION:
        # Future versions can be handled here; for now, cold-start.
        return None
    return payload
