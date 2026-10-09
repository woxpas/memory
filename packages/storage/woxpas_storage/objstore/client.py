"""Encrypted S3-compatible object store client (MinIO offline → S3 in cloud).

Three buckets, three URI conventions:

    s3://woxpas-raw/{sha256_hex}                  raw original bytes
    s3://woxpas-canonical/{doc_id}/v{n}.json      versioned CDM JSON
    s3://woxpas-summaries/{doc_id}/v{n}.json      summary snapshots (optional)

Every object is client-side encrypted with AES-256-GCM under a field key
derived from the tenant DEK (HKDF). The IV travels with the blob; the field
key version is bound via AAD so re-keying surfaces as decrypt failure
rather than silent data corruption.

Content addressing for raw bytes: same input → same key → automatic dedup
across re-uploads of identical content. The caller computes SHA-256 (or we
compute it for them via put_raw_bytes()).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import aioboto3
import structlog
from botocore.exceptions import ClientError

from woxpas_storage.objstore.crypto import (
    EnvelopeBlob,
    FieldKey,
    decrypt,
    derive_field_key,
    encrypt,
    make_aad,
    sha256_bytes,
    sha256_hex,
)
from woxpas_storage.settings import ObjectStoreSettings

log = structlog.get_logger(__name__)


PURPOSE_RAW       = "obj.raw"
PURPOSE_CANONICAL = "obj.canonical"
PURPOSE_SUMMARY   = "obj.summary"


class ObjectStore:
    """S3-compatible client with client-side encryption.

    The DEK passed into per-call methods is the tenant's session DEK. The store
    never persists the DEK; field keys are derived per-call and discarded.
    """

    def __init__(self, settings: ObjectStoreSettings) -> None:
        self._settings = settings
        self._session = aioboto3.Session(
            aws_access_key_id=settings.root_user,
            aws_secret_access_key=settings.root_password.get_secret_value(),
            region_name=settings.region,
        )

    # ─── lifecycle ────────────────────────────────────────────────────────

    def _client_ctx(self) -> Any:
        return self._session.client(
            "s3",
            endpoint_url=self._settings.endpoint_url,
            use_ssl=self._settings.use_tls,
        )

    async def bootstrap(self) -> dict[str, str]:
        """Idempotent bucket creation."""
        actions: dict[str, str] = {}
        async with self._client_ctx() as s3:
            for bucket in (
                self._settings.bucket_raw,
                self._settings.bucket_canonical,
                self._settings.bucket_summaries,
            ):
                try:
                    await s3.head_bucket(Bucket=bucket)
                    actions[bucket] = "exists"
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    if code in ("404", "NoSuchBucket", "NotFound"):
                        await s3.create_bucket(Bucket=bucket)
                        actions[bucket] = "created"
                    else:
                        raise
        log.info("objstore.bootstrap", **actions)
        return actions

    async def health(self) -> dict[str, Any]:
        try:
            async with self._client_ctx() as s3:
                await s3.list_buckets()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ─── raw bytes (content-addressed) ───────────────────────────────────

    async def put_raw_bytes(
        self, content: bytes, dek: bytes, *,
        content_type: str | None = None,
    ) -> tuple[str, bytes, bytes]:
        """Store content. Returns (uri, sha256_bytes, iv).

        Idempotent: identical content → identical key → S3 overwrite is a no-op
        for our purposes (same plaintext → same hash → caller uses existing row).
        """
        digest = sha256_hex(content)
        bucket = self._settings.bucket_raw
        key = digest
        field_key = derive_field_key(dek, PURPOSE_RAW)
        aad = make_aad(bucket, key, field_key.version)
        envelope = encrypt(content, field_key, aad)

        async with self._client_ctx() as s3:
            extra: dict[str, Any] = {
                "Metadata": {
                    "x-woxpas-iv": envelope.iv.hex(),
                    "x-woxpas-fkv": str(field_key.version),
                    "x-woxpas-sha256": digest,
                }
            }
            if content_type:
                extra["ContentType"] = content_type
            await s3.put_object(
                Bucket=bucket, Key=key,
                Body=envelope.ciphertext_with_tag,
                **extra,
            )
        uri = f"s3://{bucket}/{key}"
        return uri, sha256_bytes(content), envelope.iv

    async def get_raw_bytes(
        self, sha256_hex_str: str, dek: bytes, *,
        field_key_version: int = 1,
    ) -> bytes:
        bucket = self._settings.bucket_raw
        key = sha256_hex_str
        field_key = derive_field_key(dek, PURPOSE_RAW, version=field_key_version)
        aad = make_aad(bucket, key, field_key.version)

        async with self._client_ctx() as s3:
            obj = await s3.get_object(Bucket=bucket, Key=key)
            iv_hex = obj.get("Metadata", {}).get("x-woxpas-iv")
            if not iv_hex:
                raise RuntimeError("missing iv metadata on object")
            ciphertext = await obj["Body"].read()
        envelope = EnvelopeBlob(iv=bytes.fromhex(iv_hex), ciphertext_with_tag=ciphertext)
        return decrypt(envelope, field_key, aad)

    # ─── canonical doc (versioned JSON) ───────────────────────────────────

    async def put_canonical(
        self, doc_id: UUID, version: int, payload: bytes, dek: bytes,
    ) -> tuple[str, bytes]:
        return await self._put_versioned(
            self._settings.bucket_canonical, PURPOSE_CANONICAL,
            doc_id, version, payload, dek,
        )

    async def get_canonical(
        self, doc_id: UUID, version: int, dek: bytes, *, field_key_version: int = 1,
    ) -> bytes:
        return await self._get_versioned(
            self._settings.bucket_canonical, PURPOSE_CANONICAL,
            doc_id, version, dek, field_key_version,
        )

    # ─── summary snapshot (optional) ──────────────────────────────────────

    async def put_summary(
        self, doc_id: UUID, version: int, payload: bytes, dek: bytes,
    ) -> tuple[str, bytes]:
        return await self._put_versioned(
            self._settings.bucket_summaries, PURPOSE_SUMMARY,
            doc_id, version, payload, dek,
        )

    # ─── shared versioned helpers ─────────────────────────────────────────

    async def _put_versioned(
        self, bucket: str, purpose: str,
        doc_id: UUID, version: int, payload: bytes, dek: bytes,
    ) -> tuple[str, bytes]:
        key = f"{doc_id}/v{version}.json"
        field_key = derive_field_key(dek, purpose)
        aad = make_aad(bucket, key, field_key.version)
        envelope = encrypt(payload, field_key, aad)

        async with self._client_ctx() as s3:
            await s3.put_object(
                Bucket=bucket, Key=key,
                Body=envelope.ciphertext_with_tag,
                Metadata={
                    "x-woxpas-iv": envelope.iv.hex(),
                    "x-woxpas-fkv": str(field_key.version),
                },
                ContentType="application/json",
            )
        return f"s3://{bucket}/{key}", envelope.iv

    async def _get_versioned(
        self, bucket: str, purpose: str,
        doc_id: UUID, version: int, dek: bytes, field_key_version: int,
    ) -> bytes:
        key = f"{doc_id}/v{version}.json"
        field_key = derive_field_key(dek, purpose, version=field_key_version)
        aad = make_aad(bucket, key, field_key.version)
        async with self._client_ctx() as s3:
            obj = await s3.get_object(Bucket=bucket, Key=key)
            iv_hex = obj.get("Metadata", {}).get("x-woxpas-iv")
            if not iv_hex:
                raise RuntimeError("missing iv metadata on object")
            ciphertext = await obj["Body"].read()
        envelope = EnvelopeBlob(iv=bytes.fromhex(iv_hex), ciphertext_with_tag=ciphertext)
        return decrypt(envelope, field_key, aad)


__all__ = ["FieldKey", "ObjectStore"]
