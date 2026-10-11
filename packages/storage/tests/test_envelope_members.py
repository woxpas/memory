"""Team-memory crypto: multiple members unlock the one shared tenant DEK.

The security invariant (Pillar 3): a tenant has exactly one DEK; each member
stores their own copy of it wrapped under a KEK derived from their own
passphrase. Adding a member must NOT change the DEK — every member must unlock
the identical key — and a wrong passphrase must never yield it.

These run the real Argon2id KDF, so they are a few seconds slower than a pure
hash test; that cost is the point (brute-force resistance).
"""

from __future__ import annotations

import pytest
from woxpas_storage.envelope import (
    MemberEnvelope,
    derive_kek_from_params,
    generate_dek,
    unwrap_dek,
    verify_passphrase,
    wrap_dek_for_passphrase,
)


def _unlock(envelope: MemberEnvelope, passphrase: str) -> bytes:
    """Reproduce the unlock path a member takes from their stored envelope."""
    kek = derive_kek_from_params(passphrase, envelope.kdf_params)
    return unwrap_dek(envelope.wrapped_dek, kek)


def test_two_members_unlock_the_same_dek() -> None:
    dek = generate_dek()
    alice = wrap_dek_for_passphrase(dek, "alice-correct-horse")
    bob = wrap_dek_for_passphrase(dek, "bob-battery-staple")

    # Distinct ciphertext + salts (no envelope reuse)...
    assert alice.wrapped_dek.encrypted_dek != bob.wrapped_dek.encrypted_dek
    assert alice.kdf_params["salt_b64"] != bob.kdf_params["salt_b64"]

    # ...yet both resolve to the identical shared DEK.
    assert _unlock(alice, "alice-correct-horse") == dek
    assert _unlock(bob, "bob-battery-staple") == dek


def test_member_passphrase_is_isolated() -> None:
    dek = generate_dek()
    alice = wrap_dek_for_passphrase(dek, "alice-pass")
    bob = wrap_dek_for_passphrase(dek, "bob-pass")

    # Each verifier accepts only its own passphrase.
    assert verify_passphrase(alice.passphrase_verifier, "alice-pass")
    assert not verify_passphrase(alice.passphrase_verifier, "bob-pass")
    assert verify_passphrase(bob.passphrase_verifier, "bob-pass")
    assert not verify_passphrase(bob.passphrase_verifier, "alice-pass")


def test_wrong_passphrase_never_unwraps() -> None:
    dek = generate_dek()
    env = wrap_dek_for_passphrase(dek, "the-real-passphrase")
    with pytest.raises(ValueError, match="unwrap failed"):
        _unlock(env, "not-the-passphrase")


def test_dek_version_is_carried_through() -> None:
    dek = generate_dek()
    env = wrap_dek_for_passphrase(dek, "pw", dek_version=3)
    assert env.wrapped_dek.dek_version == 3
    # unwrap AAD is version-bound, so a v3 envelope unwraps cleanly at v3.
    assert _unlock(env, "pw") == dek


def test_rejects_wrong_dek_length() -> None:
    with pytest.raises(ValueError, match="DEK must be"):
        wrap_dek_for_passphrase(b"too-short", "pw")
