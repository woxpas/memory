"""Team-memory Phase A3: role hierarchy semantics.

The gate's one rule: a None role (no member context — single-user / dev) is
FULL access, so those installs are unaffected; otherwise viewer < member <
admin < owner, and an unknown role string fails closed."""

from __future__ import annotations

from woxpas_storage import ROLE_RANK, role_allows


def test_none_role_is_full_access() -> None:
    # Single-user / dev installs have no member context → allowed everywhere.
    for minimum in ("viewer", "member", "admin", "owner"):
        assert role_allows(None, minimum) is True


def test_hierarchy() -> None:
    assert role_allows("viewer", "viewer")
    assert not role_allows("viewer", "member")
    assert role_allows("member", "member")
    assert not role_allows("member", "admin")
    assert role_allows("admin", "member")
    assert role_allows("owner", "admin")
    assert role_allows("owner", "owner")


def test_unknown_role_fails_closed() -> None:
    assert not role_allows("superuser", "viewer")
    assert not role_allows("", "viewer")


def test_rank_order() -> None:
    assert ROLE_RANK["viewer"] < ROLE_RANK["member"] < ROLE_RANK["admin"] < ROLE_RANK["owner"]
