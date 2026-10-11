"""ScopeAdminEngine — Phase 5a.5.0 Group A.

Wraps repo-layer scope operations + Qdrant scopes_collection upserts behind
a tenant-isolated, idempotency-aware engine surface. Powers HTTP routes
(/scopes, /documents/{id}/scopes) and, via thin tool wrappers, the MCP
server's `memory.create_scope` / `memory.assign_to_scope` /
`memory.unassign_from_scope` tools.

Why an engine layer (not direct repo calls in the route):
  - Embedding scope name+description into Qdrant is non-trivial: requires
    embedder, content_uuid, ScopePoint, plus rollback semantics if Qdrant
    fails after the SQL row is created.
  - Idempotency keys for state-machine writes (per Phase 5a.5 plan).
  - Audit log integration lives here so HTTP and MCP both get it free.

Three-track scope model (project_phase5a5_mcp_plan.md §5):
  - create_scope and assign_to_scope are Track 2's "agent should-scope"
    primitives — agents call these mid-conversation when no existing
    scope matches the conversation topic.
  - unassign_from_scope supports the Track 1 standalone case where a
    user re-organizes manually.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

if TYPE_CHECKING:
    from woxpas_runtime import EmbedderBackend
    from woxpas_storage import PostgresPool, QdrantClient

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CreateScopeResult:
    scope_id:        UUID
    slug:            str
    embedded:        bool
    embedding_ref:   UUID | None
    already_existed: bool
    """True when the (tenant_id, slug) combo already had a row; ON CONFLICT
    DO UPDATE behavior preserves it. Caller decides whether to surface
    that to the user."""


@dataclass(slots=True)
class ScopeMutationResult:
    scope_id:    UUID
    doc_id:      UUID
    inserted:    bool
    """For assign: True if a NEW assignment was made; False if already
    present (idempotent). For unassign: True if a row was removed."""


class ScopeAdminEngine:
    """Manage scopes + doc-scope assignments. Thread-safe across requests
    (no per-request mutable state held)."""

    def __init__(
        self, *, pool: PostgresPool, qdrant: QdrantClient, embedder: EmbedderBackend,
    ) -> None:
        self._pool     = pool
        self._qdrant   = qdrant
        self._embedder = embedder

    # ─── Create ────────────────────────────────────────────────────────────

    async def create_scope(
        self, *,
        tenant_id:       UUID,
        name:            str,
        description:     str | None = None,
        slug:            str | None = None,
        parent_scope_id: UUID | None = None,
        color:           str | None = None,
        icon:            str | None = None,
        embed:           bool = True,
        visibility:      str = "team",
        owner_member_id: UUID | None = None,
    ) -> CreateScopeResult:
        """Create (or upsert by slug) a scope. When embed=True (default)
        and there's text to embed (name + optional description), the
        scope's description vector + bootstrap centroid get upserted to
        Qdrant scopes_collection so the summarize stage's auto-assign
        can find it.

        Set embed=False ONLY for catch-all scopes like 'Inbox' that
        should NEVER auto-claim docs (no embedding = invisible to
        auto-assign).
        """
        from woxpas_core.ids import content_uuid
        from woxpas_storage import ScopePoint, ScopeRepo

        t0 = time.monotonic()

        # 1. Insert the scope row (or upsert if slug collides)
        async with self._pool.tenant_session(tenant_id) as conn:
            scope_repo = ScopeRepo(conn)
            # Detect existing-by-slug for the already_existed flag
            resolved_slug = slug or _slugify(name)
            pre = await scope_repo.by_slug(tenant_id, resolved_slug)
            already = pre is not None
            scope_id = await scope_repo.create(
                tenant_id, name,
                description=description,
                slug=resolved_slug,
                parent_scope_id=parent_scope_id,
                color=color,
                icon=icon,
                visibility=visibility,
                owner_member_id=owner_member_id,
            )

        # 2. Embed + Qdrant upsert (best-effort; logs but doesn't roll
        # back the SQL row on failure — the scope is still usable for
        # explicit assignment via assign_to_scope).
        embedding_ref: UUID | None = None
        if embed:
            text = (name or "")
            if description:
                text = f"{text}: {description}"
            text = text.strip()
            if text:
                try:
                    batch = await self._embedder.embed([text], batch_size=1)
                    if batch.dense:
                        embedding_ref = content_uuid(
                            "scope", str(scope_id),
                            self._embedder.model_version,
                        )
                        await self._qdrant.upsert_scope(ScopePoint(
                            point_id=embedding_ref,
                            tenant_id=tenant_id,
                            scope_id=scope_id,
                            member_count=0,
                            description_vector=batch.dense[0],
                            centroid_vector=batch.dense[0],
                        ))
                        async with self._pool.tenant_session(tenant_id) as conn:
                            await ScopeRepo(conn).update_embedding_ref(
                                tenant_id, scope_id, embedding_ref,
                            )
                except Exception as e:
                    log.warning("scope_admin.embed_failed",
                                tenant_id=str(tenant_id),
                                scope_id=str(scope_id),
                                error_class=type(e).__name__,
                                error=str(e)[:200])
                    embedding_ref = None

        log.info("scope_admin.create_scope.done",
                 tenant_id=str(tenant_id),
                 scope_id=str(scope_id),
                 slug=resolved_slug,
                 embedded=embedding_ref is not None,
                 already_existed=already,
                 ms=round((time.monotonic() - t0) * 1000, 1))

        return CreateScopeResult(
            scope_id=scope_id,
            slug=resolved_slug,
            embedded=embedding_ref is not None,
            embedding_ref=embedding_ref,
            already_existed=already,
        )

    # ─── Assign ────────────────────────────────────────────────────────────

    async def assign_to_scope(
        self, *,
        tenant_id: UUID,
        doc_id:    UUID,
        scope_slug: str,
        assigned_by: str = "agent",
    ) -> ScopeMutationResult:
        """Attach doc to scope (by slug). Idempotent — re-assigning is a
        no-op (returns inserted=False). Multi-scope membership allowed
        (existing schema is `scope_slugs[]`).

        `assigned_by` defaults to 'agent' since this surface is primarily
        agent-driven; route handlers can override based on auth context.
        Constraint accepts: user | classifier | agent | adapter | rule.
        """
        from woxpas_storage import DocScopeRepo, ScopeRepo

        async with self._pool.tenant_session(tenant_id) as conn:
            scope = await ScopeRepo(conn).by_slug(tenant_id, scope_slug)
            if scope is None:
                raise ValueError(f"scope not found: {scope_slug}")
            scope_id = scope["scope_id"]

            # Verify doc exists (else the FK would fail with a less clear
            # error message)
            doc_exists = await conn.fetchval(
                "SELECT 1 FROM documents "
                " WHERE tenant_id = $1 AND doc_id = $2 AND deleted_at IS NULL",
                tenant_id, doc_id,
            )
            if not doc_exists:
                raise ValueError(f"doc not found: {doc_id}")

            doc_scope = DocScopeRepo(conn)
            inserted = await doc_scope.assign(
                tenant_id, doc_id, scope_id,
                assigned_by=assigned_by,
                auto_assigned=False,
                confidence=None,
            )
            if inserted:
                await ScopeRepo(conn).member_count_delta(
                    tenant_id, scope_id, +1,
                )

        log.info("scope_admin.assign.done",
                 tenant_id=str(tenant_id),
                 doc_id=str(doc_id), scope_id=str(scope_id),
                 inserted=inserted, assigned_by=assigned_by)
        return ScopeMutationResult(
            scope_id=scope_id, doc_id=doc_id, inserted=inserted,
        )

    # ─── Unassign ──────────────────────────────────────────────────────────

    async def unassign_from_scope(
        self, *,
        tenant_id: UUID,
        doc_id:    UUID,
        scope_slug: str,
    ) -> ScopeMutationResult:
        """Remove doc-scope membership. Idempotent — calling on a non-
        existent assignment is a no-op (returns inserted=False, which in
        unassign context means 'no row was removed').

        Three-track model: this is valid because unscoped docs are
        allowed (Track 1). Resurfacing's doc_unscoped opportunity will
        re-surface the doc after unassign.
        """
        from woxpas_storage import DocScopeRepo, ScopeRepo

        async with self._pool.tenant_session(tenant_id) as conn:
            scope = await ScopeRepo(conn).by_slug(tenant_id, scope_slug)
            if scope is None:
                raise ValueError(f"scope not found: {scope_slug}")
            scope_id = scope["scope_id"]

            # Detect whether the assignment existed before delete (so we
            # know whether to decrement member_count)
            existing = await conn.fetchval(
                "SELECT 1 FROM doc_scopes "
                " WHERE tenant_id = $1 AND doc_id = $2 AND scope_id = $3",
                tenant_id, doc_id, scope_id,
            )

            if existing:
                await DocScopeRepo(conn).unassign(tenant_id, doc_id, scope_id)
                await ScopeRepo(conn).member_count_delta(
                    tenant_id, scope_id, -1,
                )

        log.info("scope_admin.unassign.done",
                 tenant_id=str(tenant_id),
                 doc_id=str(doc_id), scope_id=str(scope_id),
                 inserted=bool(existing))
        return ScopeMutationResult(
            scope_id=scope_id, doc_id=doc_id, inserted=bool(existing),
        )


def _slugify(name: str) -> str:
    """Match the slug-generation rule in scopes.create — lowercase,
    spaces to dashes, alnum + dashes only."""
    import re
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "scope"
