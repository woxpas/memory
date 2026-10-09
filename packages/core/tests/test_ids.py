"""Unit tests for content-derived UUIDs (woxpas_core.ids).

These IDs are load-bearing: re-ingesting the same content must converge on the
same UUID, or span bookmarks and entity-mention rows break across pipeline
upgrades. So determinism and collision-resistance are tested explicitly.
"""

from __future__ import annotations

import uuid

from woxpas_core.ids import content_uuid


def test_deterministic_same_inputs_same_uuid() -> None:
    assert content_uuid("doc", 7, b"body") == content_uuid("doc", 7, b"body")


def test_different_inputs_differ() -> None:
    assert content_uuid("a") != content_uuid("b")
    assert content_uuid("a", "b") != content_uuid("a", "c")


def test_unit_separator_prevents_concatenation_collision() -> None:
    # Without a separator, ("a", "b") and ("ab",) would hash identically.
    assert content_uuid("a", "b") != content_uuid("ab")


def test_version_and_variant_bits() -> None:
    u = content_uuid("x", "y")
    # Custom version 8 (the high nibble of byte 6 → the 13th hex digit).
    assert u.hex[12] == "8"
    # RFC 4122 variant: the two high bits of byte 8 are 10.
    assert (u.bytes[8] & 0xC0) == 0x80


def test_mixed_part_types_accepted() -> None:
    u = content_uuid("s", 42, b"\x00\x01")
    assert isinstance(u, uuid.UUID)


def test_int_and_str_parts_distinguished_by_value() -> None:
    # 1 (int) is stringified to "1" for hashing, matching str "1" — documented
    # behaviour; this test pins it so a future change is a conscious one.
    assert content_uuid(1) == content_uuid("1")
