"""Tenant members repository (team memory, Phase A).

A tenant is a team; each member stores their own wrapped copy of the one shared
tenant DEK. This repo handles the membership rows and the per-member envelope
ciphertext only — deriving KEKs and (un)wrapping the DEK is the caller's job
(see woxpas_storage.envelope). We never see a plaintext DEK or passphrase.

RLS context (app.tenant_id) is the caller's responsibility, as with every repo.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import asyncpg


class MemberRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def create(
        self,
        tenant_id: UUID,
        *,
        display_name: str,
        passphrase_verifier: bytes,
        kdf_params: dict[str, Any],
        encrypted_dek: bytes,
        encrypted_dek_iv: bytes,
        encrypted_dek_tag: bytes,
        dek_version: int = 1,
        role: str = "member",
        external_subject: str | None = None,
        self_entity_id: UUID | None = None,
        invited_by: UUID | None = None,
    ) -> UUID:
        """Insert a member row. The envelope fields must already wrap the SAME
        tenant DEK under this member's KEK (see envelope.wrap_dek_for_passphrase)."""
        row = await self._conn.fetchrow(
            """
            INSERT INTO tenant_members (
              tenant_id, display_name, external_subject, self_entity_id,
              role, passphrase_verifier, kdf_params,
              encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version,
              invited_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12)
            RETURNING member_id
            """,
            tenant_id, display_name, external_subject, self_entity_id,
            role, passphrase_verifier, kdf_params,
            encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version,
            invited_by,
        )
        return cast("UUID", row["member_id"])

    async def list_members(self, tenant_id: UUID, *, include_removed: bool = False) -> list[dict[str, Any]]:
        """Roster (no secret columns). Ordered owners first, then by join time."""
        rows = await self._conn.fetch(
            """
            SELECT member_id, display_name, external_subject, self_entity_id,
                   role, status, dek_version, invited_by, created_at, updated_at
              FROM tenant_members
             WHERE tenant_id = $1
               AND ($2 OR status <> 'removed')
             ORDER BY (role = 'owner') DESC, created_at ASC
            """,
            tenant_id, include_removed,
        )
        return [dict(r) for r in rows]

    async def get(self, tenant_id: UUID, member_id: UUID) -> dict[str, Any] | None:
        """One member, no secret columns."""
        row = await self._conn.fetchrow(
            """
            SELECT member_id, display_name, external_subject, self_entity_id,
                   role, status, dek_version, invited_by, created_at, updated_at
              FROM tenant_members
             WHERE tenant_id = $1 AND member_id = $2
            """,
            tenant_id, member_id,
        )
        return dict(row) if row else None

    async def get_by_subject(
        self, tenant_id: UUID, external_subject: str,
    ) -> dict[str, Any] | None:
        """Resolve a JWT subject to an active member (used by auth, Phase A2)."""
        row = await self._conn.fetchrow(
            """
            SELECT member_id, display_name, self_entity_id, role, status
              FROM tenant_members
             WHERE tenant_id = $1 AND external_subject = $2
            """,
            tenant_id, external_subject,
        )
        return dict(row) if row else None

    async def get_envelope(self, tenant_id: UUID, member_id: UUID) -> dict[str, Any] | None:
        """Fetch a member's wrapped-DEK envelope so the caller can unlock it.

        Returns the ciphertext + KDF params; the caller derives the KEK from the
        member's passphrase and unwraps. Returned only for 'active' members.
        """
        row = await self._conn.fetchrow(
            """
            SELECT passphrase_verifier, kdf_params,
                   encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version
              FROM tenant_members
             WHERE tenant_id = $1 AND member_id = $2 AND status = 'active'
            """,
            tenant_id, member_id,
        )
        return dict(row) if row else None

    async def find_active_envelopes(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """All active members' envelopes + identity, for passphrase-based unlock
        where the caller doesn't yet know which member they are (CLI unlock)."""
        rows = await self._conn.fetch(
            """
            SELECT member_id, display_name, role,
                   passphrase_verifier, kdf_params,
                   encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version
              FROM tenant_members
             WHERE tenant_id = $1 AND status = 'active'
             ORDER BY (role = 'owner') DESC, created_at ASC
            """,
            tenant_id,
        )
        return [dict(r) for r in rows]

    async def set_status(self, tenant_id: UUID, member_id: UUID, status: str) -> bool:
        """Suspend / remove / reactivate. Removing revokes the member's ability to
        unwrap the DEK (their envelope row is kept for audit unless hard-deleted;
        forward secrecy via DEK rotation is a later sub-phase)."""
        result: str = await self._conn.execute(
            "UPDATE tenant_members SET status = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND member_id = $2",
            tenant_id, member_id, status,
        )
        return result.endswith(" 1")

    async def set_role(self, tenant_id: UUID, member_id: UUID, role: str) -> bool:
        result: str = await self._conn.execute(
            "UPDATE tenant_members SET role = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND member_id = $2",
            tenant_id, member_id, role,
        )
        return result.endswith(" 1")

    async def count_active_owners(self, tenant_id: UUID) -> int:
        """Guard against removing/demoting the last owner of a team."""
        row = await self._conn.fetchrow(
            "SELECT count(*) AS n FROM tenant_members "
            "WHERE tenant_id = $1 AND role = 'owner' AND status = 'active'",
            tenant_id,
        )
        return int(row["n"]) if row else 0
