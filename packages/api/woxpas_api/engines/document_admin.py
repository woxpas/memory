"""DocumentAdminEngine — Phase 5a.5.0 Group B.

Document state-change operations: archive / unarchive.

Archived docs are excluded from retrieval (existing pipeline filter
checks `archived_at IS NULL` in v_documents and the chunks/summaries
filtering paths). Unarchive flips them back to live.

Hard delete is INTENTIONALLY not in v0.1 (project_phase5a5_mcp_plan.md
non-goals): UX requires careful handling of cascade + Qdrant cleanup +
audit trail. Archive is the user-facing 'remove from view' affordance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class DocumentStateResult:
    doc_id:       UUID
    state_changed: bool
    """True if the document's archive state actually flipped this call.
    False = no-op (already in target state — idempotent)."""
    archived_at:   str | None
    """ISO timestamp string, or None if not archived. Returned post-
    transition so the caller can confirm/display."""


class DocumentAdminEngine:
    """Archive + unarchive operations. Tenant-isolated via tenant_session."""

    def __init__(self, *, pool) -> None:                      # type: ignore[no-untyped-def]
        self._pool = pool

    async def archive_document(
        self, *, tenant_id: UUID, doc_id: UUID, reason: str | None = None,
    ) -> DocumentStateResult:
        """Set archived_at = now() if currently NULL. Idempotent —
        re-archiving an already-archived doc is a no-op."""
        t0 = time.monotonic()
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                UPDATE documents
                   SET archived_at = now(), updated_at = now()
                 WHERE tenant_id   = $1
                   AND doc_id      = $2
                   AND deleted_at  IS NULL
                   AND archived_at IS NULL
                RETURNING archived_at
                """,
                tenant_id, doc_id,
            )
            if row is not None:
                # Audit via timeline (the existing pattern for state events)
                from woxpas_storage import TimelineRepo
                payload = {"reason": reason} if reason else {}
                await TimelineRepo(conn).record(
                    tenant_id, "doc.summarized",      # closest existing event_type
                    doc_id=doc_id, actor="user",
                    payload={"stage": "archive", **payload},
                )
                state_changed = True
                archived_at = row["archived_at"]
            else:
                # Either the doc doesn't exist (under this tenant) or was
                # already archived. Probe to distinguish.
                existing = await conn.fetchrow(
                    "SELECT archived_at FROM documents "
                    "WHERE tenant_id = $1 AND doc_id = $2 AND deleted_at IS NULL",
                    tenant_id, doc_id,
                )
                if existing is None:
                    raise ValueError(f"document not found: {doc_id}")
                state_changed = False
                archived_at = existing["archived_at"]

        log.info("document_admin.archive.done",
                 tenant_id=str(tenant_id), doc_id=str(doc_id),
                 state_changed=state_changed,
                 ms=round((time.monotonic() - t0) * 1000, 1))
        return DocumentStateResult(
            doc_id=doc_id,
            state_changed=state_changed,
            archived_at=archived_at.isoformat() if archived_at else None,
        )

    async def unarchive_document(
        self, *, tenant_id: UUID, doc_id: UUID,
    ) -> DocumentStateResult:
        """Clear archived_at if currently set. Idempotent."""
        t0 = time.monotonic()
        async with self._pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                UPDATE documents
                   SET archived_at = NULL, updated_at = now()
                 WHERE tenant_id   = $1
                   AND doc_id      = $2
                   AND deleted_at  IS NULL
                   AND archived_at IS NOT NULL
                RETURNING doc_id
                """,
                tenant_id, doc_id,
            )
            if row is not None:
                from woxpas_storage import TimelineRepo
                await TimelineRepo(conn).record(
                    tenant_id, "doc.summarized",
                    doc_id=doc_id, actor="user",
                    payload={"stage": "unarchive"},
                )
                state_changed = True
            else:
                existing = await conn.fetchval(
                    "SELECT 1 FROM documents "
                    "WHERE tenant_id = $1 AND doc_id = $2 AND deleted_at IS NULL",
                    tenant_id, doc_id,
                )
                if not existing:
                    raise ValueError(f"document not found: {doc_id}")
                state_changed = False

        log.info("document_admin.unarchive.done",
                 tenant_id=str(tenant_id), doc_id=str(doc_id),
                 state_changed=state_changed,
                 ms=round((time.monotonic() - t0) * 1000, 1))
        return DocumentStateResult(
            doc_id=doc_id, state_changed=state_changed, archived_at=None,
        )
