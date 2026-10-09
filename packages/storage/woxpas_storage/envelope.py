"""Tenant DEK envelope encryption + passphrase verification.

The model:

  passphrase ──Argon2id KDF──▶ KEK (32B)
                                   │
                                   ▼
                          AES-256-GCM unwraps DEK (32B)
                                   │
                                   ▼
            field keys (HKDF-derived per purpose) for object store + credentials

Two Argon2id usages:

  1. KDF (raw): derives the KEK from the passphrase. Slow on purpose
     (high time/memory cost). Re-runs every unlock.

  2. Verifier hash: standard PHC string format. Lets us check a passphrase
     attempt without first deriving the (expensive) KEK — useful for
     rate-limited unlock attempts and for verifying recovery keys.

DEK is generated once at tenant-init time, stored only as ciphertext under the
KEK. Passphrase rotation re-derives KEK with a new salt and re-wraps the DEK
(no data re-encryption needed).
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exc
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ─── Argon2id parameters ───────────────────────────────────────────────────
# KDF (KEK derivation) parameters. Tuned for ~500ms on a modern laptop CPU.
# Re-tune via env in production; the params are persisted per-tenant so changes
# don't break existing tenants.
DEFAULT_KDF_TIME_COST    = 4
DEFAULT_KDF_MEMORY_KIB   = 262_144      # 256 MiB
DEFAULT_KDF_PARALLELISM  = 2
DEFAULT_KDF_SALT_LEN     = 16
KEK_LEN                  = 32
DEK_LEN                  = 32
ENV_AAD                  = b"woxpas/dek/v1"


@dataclass(slots=True)
class WrappedDek:
    """Persisted form of a tenant DEK."""
    encrypted_dek: bytes        # ciphertext (DEK_LEN bytes)
    iv:            bytes        # GCM IV (12 bytes)
    tag:           bytes        # GCM auth tag (16 bytes)
    kdf_params:    dict[str, Any]
    dek_version:   int = 1


@dataclass(slots=True)
class UnlockedTenant:
    """In-memory unlock result. Caller is responsible for zeroizing `dek`."""
    dek: bytes
    kek: bytes
    dek_version: int


# ─── KDF ───────────────────────────────────────────────────────────────────

def derive_kek(
    passphrase: str | bytes,
    *,
    salt: bytes,
    time_cost:   int = DEFAULT_KDF_TIME_COST,
    memory_kib:  int = DEFAULT_KDF_MEMORY_KIB,
    parallelism: int = DEFAULT_KDF_PARALLELISM,
) -> bytes:
    """Argon2id raw KDF → KEK (32 bytes)."""
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    return hash_secret_raw(
        secret=passphrase, salt=salt,
        time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism,
        hash_len=KEK_LEN, type=Type.ID,
    )


def make_kdf_params(
    *,
    time_cost:   int = DEFAULT_KDF_TIME_COST,
    memory_kib:  int = DEFAULT_KDF_MEMORY_KIB,
    parallelism: int = DEFAULT_KDF_PARALLELISM,
    salt_len:    int = DEFAULT_KDF_SALT_LEN,
) -> tuple[dict[str, Any], bytes]:
    """Generate fresh KDF params + salt for a new tenant."""
    salt = secrets.token_bytes(salt_len)
    return {
        "algo":        "argon2id",
        "version":     1,
        "time_cost":   time_cost,
        "memory_kib":  memory_kib,
        "parallelism": parallelism,
        "salt_b64":    base64.standard_b64encode(salt).decode("ascii"),
    }, salt


def derive_kek_from_params(passphrase: str | bytes, kdf_params: dict[str, Any]) -> bytes:
    """Resurrect KEK using the persisted params + salt."""
    salt = base64.standard_b64decode(kdf_params["salt_b64"])
    return derive_kek(
        passphrase, salt=salt,
        time_cost=int(kdf_params["time_cost"]),
        memory_kib=int(kdf_params["memory_kib"]),
        parallelism=int(kdf_params["parallelism"]),
    )


# ─── DEK wrap / unwrap ─────────────────────────────────────────────────────

def generate_dek() -> bytes:
    return secrets.token_bytes(DEK_LEN)


def wrap_dek(dek: bytes, kek: bytes, *, dek_version: int = 1) -> WrappedDek:
    if len(dek) != DEK_LEN:
        raise ValueError(f"DEK must be {DEK_LEN} bytes")
    if len(kek) != KEK_LEN:
        raise ValueError(f"KEK must be {KEK_LEN} bytes")
    iv = secrets.token_bytes(12)
    aad = ENV_AAD + f"|v{dek_version}".encode("ascii")
    ct_with_tag = AESGCM(kek).encrypt(iv, dek, aad)
    return WrappedDek(
        encrypted_dek=ct_with_tag[:-16],
        iv=iv,
        tag=ct_with_tag[-16:],
        kdf_params={},
        dek_version=dek_version,
    )


def unwrap_dek(
    wrapped: WrappedDek, kek: bytes,
) -> bytes:
    aad = ENV_AAD + f"|v{wrapped.dek_version}".encode("ascii")
    ct_with_tag = wrapped.encrypted_dek + wrapped.tag
    try:
        return AESGCM(kek).decrypt(wrapped.iv, ct_with_tag, aad)
    except Exception as e:
        raise ValueError("DEK unwrap failed (wrong passphrase or tampered envelope)") from e


# ─── Passphrase verifier (PHC string) ──────────────────────────────────────

_VERIFIER_PH = PasswordHasher(
    time_cost=2, memory_cost=65_536, parallelism=2, hash_len=32, salt_len=16,
)


def hash_verifier(passphrase: str) -> bytes:
    """Return PHC-formatted verifier (utf-8 bytes ready for bytea column)."""
    return _VERIFIER_PH.hash(passphrase).encode("utf-8")


def verify_passphrase(verifier: bytes, passphrase: str) -> bool:
    try:
        _VERIFIER_PH.verify(verifier.decode("utf-8"), passphrase)
        return True
    except (argon2_exc.VerifyMismatchError, argon2_exc.InvalidHashError):
        return False
    except Exception:
        return False


def needs_rehash(verifier: bytes) -> bool:
    """True if the verifier was made with weaker params than the current default
    and should be regenerated on next successful unlock."""
    try:
        return _VERIFIER_PH.check_needs_rehash(verifier.decode("utf-8"))
    except Exception:
        return True


# ─── Convenience: end-to-end tenant init ──────────────────────────────────

@dataclass(slots=True)
class FreshTenantSecrets:
    """Bundle returned by init_tenant_secrets() — caller persists fields and
    immediately discards `dek` (or hands it to a session/boot service)."""
    dek:                bytes
    wrapped_dek:        WrappedDek
    kdf_params:         dict[str, Any]
    passphrase_verifier: bytes


def init_tenant_secrets(passphrase: str) -> FreshTenantSecrets:
    kdf_params, salt = make_kdf_params()
    kek = derive_kek(
        passphrase, salt=salt,
        time_cost=int(kdf_params["time_cost"]),
        memory_kib=int(kdf_params["memory_kib"]),
        parallelism=int(kdf_params["parallelism"]),
    )
    dek = generate_dek()
    wrapped = wrap_dek(dek, kek)
    return FreshTenantSecrets(
        dek=dek,
        wrapped_dek=wrapped,
        kdf_params=kdf_params,
        passphrase_verifier=hash_verifier(passphrase),
    )
