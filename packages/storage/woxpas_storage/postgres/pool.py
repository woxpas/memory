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
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from woxpas_storage.settings import PostgresSettings

log = structlog.get_logger(__name__)


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
    async def tenant_session(self, tenant_id: UUID) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a connection scoped to a tenant via RLS.

        Opens an outer transaction (so SET LOCAL app.tenant_id sticks) and
        switches role to woxpas_app for least-privilege query execution.
        Caller may open inner sub-transactions with `conn.transaction()`.
        """
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
