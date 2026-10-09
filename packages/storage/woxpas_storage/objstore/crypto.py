"""Client-side AES-256-GCM envelope encryption for object store payloads.

The DEK is held in mlock'd memory by the boot service / session. Each object
gets a per-object IV (96-bit, random) and a 128-bit auth tag. Layout on the wire:

    blob = IV (12B) || ciphertext || auth_tag (16B)

GCM authenticates the AAD too. We bind:
  - the bucket name
  - the object key (sha256 hex for raw, doc_id+version for canonical)
  - the field_key_version (for rotation)

If a key is rotated, AAD mismatch surfaces immediately on decrypt.

For v0.1 we encrypt in memory (acceptable for thoughts/voice notes/PDFs up to
hundreds of MB). For very large files, switch to chunked AES-CTR + streaming
HMAC; the public encrypt/decrypt API stays the same.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from hashlib import sha256
from typing import NamedTuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

IV_LEN = 12          # GCM standard
TAG_LEN = 16
KEY_LEN = 32         # AES-256


class EnvelopeBlob(NamedTuple):
    """Raw bytes layout: iv || ciphertext_with_tag.

    Serializable as a single byte blob for object-store put.
    """
    iv: bytes
    ciphertext_with_tag: bytes

    def to_bytes(self) -> bytes:
        return self.iv + self.ciphertext_with_tag

    @classmethod
    def from_bytes(cls, raw: bytes) -> EnvelopeBlob:
        if len(raw) < IV_LEN + TAG_LEN:
            raise ValueError("envelope blob too short")
        return cls(iv=raw[:IV_LEN], ciphertext_with_tag=raw[IV_LEN:])


@dataclass(slots=True, frozen=True)
class FieldKey:
    """A purpose-bound key derived from the tenant DEK via HKDF.

    The DEK never leaves the secure session memory; we derive scoped subkeys
    for each purpose so a single subkey leak doesn't expose other ciphertexts.
    """
    key: bytes
    version: int

    def __post_init__(self) -> None:
        if len(self.key) != KEY_LEN:
            raise ValueError("field key must be 32 bytes (AES-256)")


def derive_field_key(dek: bytes, purpose: str, version: int = 1) -> FieldKey:
    """HKDF-SHA256 from the tenant DEK. Different purpose strings → different keys."""
    if len(dek) < KEY_LEN:
        raise ValueError("DEK must be at least 32 bytes")
    info = f"woxpas/v1/{purpose}/v{version}".encode()
    okm = HKDF(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=None, info=info,
    ).derive(dek)
    return FieldKey(key=okm, version=version)


def make_aad(bucket: str, object_key: str, field_key_version: int) -> bytes:
    """Stable additional authenticated data — bound to where the blob lives."""
    return f"woxpas|v1|{bucket}|{object_key}|kv={field_key_version}".encode()


def encrypt(plaintext: bytes, key: FieldKey, aad: bytes) -> EnvelopeBlob:
    iv = os.urandom(IV_LEN)
    aes = AESGCM(key.key)
    ct = aes.encrypt(iv, plaintext, aad)
    return EnvelopeBlob(iv=iv, ciphertext_with_tag=ct)


def decrypt(blob: EnvelopeBlob, key: FieldKey, aad: bytes) -> bytes:
    aes = AESGCM(key.key)
    return aes.decrypt(blob.iv, blob.ciphertext_with_tag, aad)


def sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def sha256_bytes(data: bytes) -> bytes:
    return sha256(data).digest()
