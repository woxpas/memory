"""Sources repository.

Tracks every capture surface (file upload, voice note, MCP server, etc.).
Credentials live in source_credentials encrypted under a field key derived
from the tenant DEK; this repo handles ciphertext only — encryption is the
caller's responsibility.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg


class SourceRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def get_or_create(
        self,
        tenant_id: UUID,
        type_: str,
        name: str,
        config: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> UUID:
        # Insert-or-return-id, idempotent by (tenant_id, type, name)
        row = await self._conn.fetchrow(
            """
            INSERT INTO sources (tenant_id, type, name, description, config)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (tenant_id, type, name) DO UPDATE
              SET updated_at = now()
            RETURNING source_id
            """,
            tenant_id, type_, name, description, config or {},
        )
        return cast("UUID", row["source_id"])

    async def get(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM sources WHERE tenant_id = $1 AND source_id = $2",
            tenant_id, source_id,
        )
        return dict(row) if row else None

    async def update_last_synced(self, tenant_id: UUID, source_id: UUID, when: datetime) -> None:
        await self._conn.execute(
            "UPDATE sources SET last_synced_at = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND source_id = $2",
            tenant_id, source_id, when,
        )

    async def update_status(
        self, tenant_id: UUID, source_id: UUID,
        status: str, error_state: dict[str, Any] | None = None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE sources
               SET status = $3, error_state = $4::jsonb, updated_at = now()
             WHERE tenant_id = $1 AND source_id = $2
            """,
            tenant_id, source_id, status, error_state,
        )


class SourceCredentialRepo:
    """Stores AES-256-GCM ciphertext + IV + tag. The caller derives the field key
    from the session DEK and encrypts/decrypts. We never see plaintext."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def upsert(
        self,
        tenant_id: UUID, source_id: UUID,
        *,
        ciphertext: bytes, iv: bytes, tag: bytes,
        field_key_version: int = 1,
        expires_at: datetime | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO source_credentials (
              source_id, tenant_id, encrypted_blob, iv, tag,
              field_key_version, expires_at, rotated_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
            ON CONFLICT (source_id) DO UPDATE
              SET encrypted_blob = EXCLUDED.encrypted_blob,
                  iv = EXCLUDED.iv, tag = EXCLUDED.tag,
                  field_key_version = EXCLUDED.field_key_version,
                  expires_at = EXCLUDED.expires_at,
                  rotated_at = now(), updated_at = now()
            """,
            source_id, tenant_id, ciphertext, iv, tag, field_key_version, expires_at,
        )

    async def get(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            "SELECT encrypted_blob, iv, tag, field_key_version, expires_at "
            "  FROM source_credentials WHERE tenant_id = $1 AND source_id = $2",
            tenant_id, source_id,
        )
        return dict(row) if row else None
