"""Postgres connection pool with RLS-aware tenant sessions.

Two access patterns:

  1. Simple acquire (no tenant context — for migrations, health checks, admin):
        async with pool.acquire() as conn: ...

  2. Tenant session (every runtime query goes through this):
        async with pool.tenant_session(tenant_id) as conn:
            async with conn.transaction():
                ...
     SET LOCAL app.tenant_id is set inside an outer transaction so RLS policies
     filter every query. The connection runs as woxpas_app (least privilege).

Connection pool tuned for low-latency hot path: min_size pre-warmed, statement
cache enabled, command timeout to fail fast on a deadlocked query.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from woxpas_storage.settings import PostgresSettings

log = structlog.get_logger(__name__)

# ─── Ambient member attribution (team memory, Phase A2) ─────────────────────
# Set by the auth/MCP layer once it has resolved WHICH member is acting; read by
# tenant_session() as a fallback so every write path records attribution via the
# column DEFAULTs (migration 033) without threading member_id through call sites.
# Per-request isolation is free: asyncio tasks each hold their own ContextVar
# snapshot, exactly like auth.py's tenant ContextVar.
_member_ctx: ContextVar[UUID | None] = ContextVar("woxpas_member_id", default=None)
# The acting member's role (team memory, Phase A3). None = no member context
# (single-user / unattributed), which the role gate treats as full access.
_role_ctx: ContextVar[str | None] = ContextVar("woxpas_member_role", default=None)


def set_current_member_id(member_id: UUID | None) -> None:
    """Record the acting member for this async context. Auth sets it per request
    (jwt mode); the MCP dispatcher sets it per tool call from WOXPAS_MCP_MEMBER_ID."""
    _member_ctx.set(member_id)


def current_member_id() -> UUID | None:
    """The acting member for this async context, or None (unattributed)."""
    return _member_ctx.get()


def set_current_member_role(role: str | None) -> None:
    """Record the acting member's role for this async context (team memory,
    Phase A3). Set together with the member id by auth / MCP dispatch."""
    _role_ctx.set(role)


def current_member_role() -> str | None:
    """The acting member's role (owner|admin|member|viewer), or None when there
    is no member context — which the role gate treats as full access
    (single-user / dev installs)."""
    return _role_ctx.get()


# Role hierarchy (team memory, Phase A3). Higher rank ⊇ lower-rank permissions.
ROLE_RANK: dict[str, int] = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


def role_allows(role: str | None, minimum: str) -> bool:
    """True if `role` meets or exceeds `minimum`.

    A None role (no member context — single-user / dev installs) is treated as
    FULL access, so these deployments are unaffected. An unknown role string
    is denied (fails closed)."""
    if role is None:
        return True
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[minimum]


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Set up codecs and search_path on every new connection."""
    # JSON / JSONB — encode as text on Python side; asyncpg returns dict natively for jsonb
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )


class PostgresPool:
    """Async Postgres pool with tenant-aware session helper."""

    def __init__(self, pool: asyncpg.Pool, settings: PostgresSettings) -> None:
        self._pool = pool
        self._settings = settings

    @classmethod
    async def from_settings(cls, settings: PostgresSettings) -> PostgresPool:
        pool = await asyncpg.create_pool(
            dsn=settings.dsn(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            max_queries=settings.pool_max_queries,
            max_inactive_connection_lifetime=settings.pool_max_inactive_connection_lifetime,
            statement_cache_size=settings.statement_cache_size,
            command_timeout=settings.command_timeout_s,
            init=_init_connection,
        )
        if pool is None:                       # asyncpg may return None on misconfig
            raise RuntimeError("failed to construct Postgres pool")
        log.info("pg_pool.created",
                 host=settings.host, db=settings.db,
                 min=settings.pool_min_size, max=settings.pool_max_size)
        return cls(pool, settings)

    async def close(self) -> None:
        await self._pool.close()
        log.info("pg_pool.closed")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Raw acquire. No tenant context. Use for admin / health / migrations."""
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def tenant_session(
        self, tenant_id: UUID, member_id: UUID | None = None,
        member_role: str | None = None,
    ) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection scoped to a tenant via RLS.

        Opens an outer transaction (so SET LOCAL app.tenant_id sticks) and
        switches role to woxpas_app for least-privilege query execution.
        Caller may open inner sub-transactions with `conn.transaction()`.

        `member_id` (team memory, Phase A2): the acting member, for attribution.
        Explicit arg wins; otherwise the ambient `current_member_id()` is used.
        When neither is present, app.member_id stays unset and attribution
        columns default to NULL (single-user / background-worker writes).

        `member_role` (team memory, Phase A4): the acting member's role, exposed
        to RLS as app.member_role so private-scope policies can grant admins/
        owners full visibility. Explicit arg wins, else ambient
        `current_member_role()`. Unset ⇒ no role in policy (full access for
        single-user / background sessions, matching member_id).
        """
        eff_member = member_id if member_id is not None else _member_ctx.get()
        eff_role = member_role if member_role is not None else _role_ctx.get()
        async with self._pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                # Switch to least-privilege role for the duration of this session.
                # SET LOCAL ROLE is reverted at COMMIT/ROLLBACK.
                # NOTE: the role name is interpolated because Postgres does not
                # accept a bind parameter for a role identifier here. It is a
                # trusted config constant (settings.app_role), never user input,
                # so there is no injection surface.
                await conn.execute(f"SET LOCAL ROLE {self._settings.app_role}")
                # RLS context: every policy reads current_setting('app.tenant_id').
                # SET LOCAL doesn't accept bind parameters; use set_config() instead,
                # which does. (Postgres docs: SET takes value tokens, not $-params.)
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                # Attribution context (read by the actor_member_id column DEFAULTs).
                if eff_member is not None:
                    await conn.execute(
                        "SELECT set_config('app.member_id', $1, true)",
                        str(eff_member),
                    )
                # Role context (read by private-scope RLS for admin/owner bypass).
                if eff_role is not None:
                    await conn.execute(
                        "SELECT set_config('app.member_role', $1, true)",
                        eff_role,
                    )
                yield conn
            except Exception:
                await tx.rollback()
                raise
            else:
                await tx.commit()

    async def health(self) -> dict[str, Any]:
        try:
            async with self._pool.acquire() as conn:
                v = await conn.fetchval("SELECT 1")
            return {"ok": v == 1, "size": self._pool.get_size(), "free": self._pool.get_idle_size()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
