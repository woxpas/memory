"""Documents repository.

Repos take an asyncpg.Connection acquired from PostgresPool.tenant_session(),
which has RLS context already set. Every query still includes tenant_id in
WHERE — defense in depth + planner hint for composite indexes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg
from woxpas_core import DocStatus, ProcessingProfile

# Columns we read back into a dict — keep stable; consumers depend on these names.
_DOC_COLUMNS = """
  doc_id, tenant_id, source_id, source_channel, captured_by, thread_id,
  raw_uri, raw_iv, canonical_uri, canonical_iv, sha256, mime, size_bytes,
  filename, title, language, page_count, duration_ms,
  processing_profile, processing_profile_version, pipeline_version, status,
  sensitivity_class, sensitivity_reason,
  av_result, av_signature, av_scanned_at,
  summary, summary_embedding_ref, summary_model_version, summary_profile_version,
  summary_generated_at, topics_inferred, doc_type_inferred, language_detected,
  system_time, valid_time,
  last_seen_at, last_mentioned_at, last_accessed_at, access_count,
  decay_score, momentum_score, surface_score,
  supersedes_doc_id, supersedes_candidate_doc_id, superseded_by_doc_id, revision_chain,
  external_refs, archived_at, deleted_at, created_at, updated_at
"""


class DocumentRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def create(
        self,
        *,
        tenant_id: UUID,
        source_id: UUID | None,
        source_channel: str,
        captured_by: str,
        raw_uri: str,
        raw_iv: bytes | None,
        sha256: bytes,
        mime: str | None,
        size_bytes: int | None,
        filename: str | None,
        suggested_title: str | None,
        processing_profile: ProcessingProfile,
        processing_profile_version: str,
        pipeline_version: str,
        thread_id: UUID | None,
        valid_time_hint: datetime | None,
        external_refs: list[dict[str, Any]],
        sensitivity_class: str | None = None,
    ) -> UUID:
        # Tier 1 manual flag: if user marked sensitive at upload, persist it
        # immediately. None → use the column default ('standard'). Auto-tiers
        # downstream may upgrade 'standard' → 'sensitive' but never downgrade.
        sens_value = sensitivity_class or "standard"
        sens_reason = "manual:user" if sensitivity_class == "sensitive" else None
        row = await self._conn.fetchrow(
            """
            INSERT INTO documents (
              tenant_id, source_id, source_channel, captured_by, thread_id,
              raw_uri, raw_iv, sha256, mime, size_bytes, filename, title,
              processing_profile, processing_profile_version, pipeline_version, status,
              valid_time, external_refs,
              sensitivity_class, sensitivity_reason
            ) VALUES (
              $1, $2, $3, $4, $5,
              $6, $7, $8, $9, $10, $11, $12,
              $13, $14, $15, 'received',
              CASE WHEN $16::timestamptz IS NULL THEN NULL
                   ELSE tstzrange($16::timestamptz, NULL, '[)') END,
              $17::jsonb,
              $18, $19
            )
            RETURNING doc_id
            """,
            tenant_id, source_id, source_channel, captured_by, thread_id,
            raw_uri, raw_iv, sha256, mime, size_bytes, filename, suggested_title,
            processing_profile.value, processing_profile_version, pipeline_version,
            valid_time_hint, external_refs,
            sens_value, sens_reason,
        )
        return cast("UUID", row["doc_id"])

    async def update_status(self, tenant_id: UUID, doc_id: UUID, status: DocStatus) -> None:
        await self._conn.execute(
            "UPDATE documents SET status = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND doc_id = $2",
            tenant_id, doc_id, status.value,
        )

    async def set_sensitivity_sensitive(
        self,
        tenant_id: UUID, doc_id: UUID, reason: str,
    ) -> None:
        """Upgrade sensitivity_class to 'sensitive' (idempotent) and append
        `reason` to sensitivity_reason. Reasons are semicolon-joined; an exact
        substring match is suppressed so re-running heuristics doesn't bloat
        the audit trail.

        This is monotonic UP-ONLY. To downgrade back to 'standard' use the
        explicit `set_sensitivity_class` admin method (mark-sensitive CLI).
        """
        await self._conn.execute(
            """
            UPDATE documents
               SET sensitivity_class = 'sensitive',
                   sensitivity_reason = CASE
                     WHEN sensitivity_reason IS NULL OR sensitivity_reason = ''
                       THEN $3
                     WHEN POSITION($3 IN sensitivity_reason) > 0
                       THEN sensitivity_reason
                     ELSE sensitivity_reason || ';' || $3
                   END,
                   updated_at = now()
             WHERE tenant_id = $1 AND doc_id = $2
            """,
            tenant_id, doc_id, reason,
        )

    async def set_sensitivity_class(
        self,
        tenant_id: UUID, doc_id: UUID,
        *,
        sensitivity_class: str, reason: str | None = None,
    ) -> None:
        """Admin-only direct write. Used by `woxpas-cli mark-sensitive` to
        downgrade or set an explicit reason. Heuristic / LLM tiers should
        use set_sensitivity_sensitive instead (monotonic up-only)."""
        await self._conn.execute(
            """
            UPDATE documents
               SET sensitivity_class  = $3,
                   sensitivity_reason = $4,
                   updated_at         = now()
             WHERE tenant_id = $1 AND doc_id = $2
            """,
            tenant_id, doc_id, sensitivity_class, reason,
        )

    async def update_av_result(
        self,
        tenant_id: UUID, doc_id: UUID,
        result: str, signature: str | None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE documents
               SET av_result = $3, av_signature = $4, av_scanned_at = now(),
                   status = CASE WHEN $3 = 'infected' THEN 'quarantined'::text
                                 ELSE status END,
                   updated_at = now()
             WHERE tenant_id = $1 AND doc_id = $2
            """,
            tenant_id, doc_id, result, signature,
        )

    async def update_canonical(
        self,
        tenant_id: UUID, doc_id: UUID,
        *,
        canonical_uri: str, canonical_iv: bytes | None,
        language: str | None, page_count: int | None, duration_ms: int | None,
        title: str | None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE documents
               SET canonical_uri = $3, canonical_iv = $4,
                   language = COALESCE($5, language),
                   page_count = COALESCE($6, page_count),
                   duration_ms = COALESCE($7, duration_ms),
                   title = COALESCE($8, title),
                   updated_at = now()
             WHERE tenant_id = $1 AND doc_id = $2
            """,
            tenant_id, doc_id, canonical_uri, canonical_iv,
            language, page_count, duration_ms, title,
        )

    async def update_summary(
        self,
        tenant_id: UUID, doc_id: UUID,
        *,
        summary: str,
        model_version: str,
        profile_version: str,
        topics: list[str],
        summary_embedding_ref: UUID | None = None,    # nullable in v0.1; populated when we add summary embeddings
        language_detected: str | None = None,
        doc_type_inferred: str | None = None,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE documents
               SET summary = $3,
                   summary_embedding_ref = COALESCE($4, summary_embedding_ref),
                   summary_model_version = $5,
                   summary_profile_version = $6,
                   summary_generated_at = now(),
                   topics_inferred = $7,
                   language_detected = COALESCE($8, language_detected),
                   doc_type_inferred = COALESCE($9, doc_type_inferred),
                   updated_at = now()
             WHERE tenant_id = $1 AND doc_id = $2
            """,
            tenant_id, doc_id, summary, summary_embedding_ref,
            model_version, profile_version, topics,
            language_detected, doc_type_inferred,
        )

    async def set_supersession_candidate(
        self, tenant_id: UUID, doc_id: UUID, candidate_id: UUID,
    ) -> None:
        await self._conn.execute(
            "UPDATE documents SET supersedes_candidate_doc_id = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND doc_id = $2",
            tenant_id, doc_id, candidate_id,
        )

    async def get(self, tenant_id: UUID, doc_id: UUID) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            f"SELECT {_DOC_COLUMNS} FROM documents "
            "WHERE tenant_id = $1 AND doc_id = $2 AND deleted_at IS NULL",
            tenant_id, doc_id,
        )
        return dict(row) if row else None

    async def by_sha256(self, tenant_id: UUID, sha256: bytes) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            f"SELECT {_DOC_COLUMNS} FROM documents "
            "WHERE tenant_id = $1 AND sha256 = $2 AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            tenant_id, sha256,
        )
        return dict(row) if row else None

    async def recent_for_supersession_check(
        self,
        tenant_id: UUID, source_channel: str,
        within_seconds: int, limit: int = 20,
        thread_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Recent docs from same channel/thread for vector-similarity revision check."""
        if thread_id is not None:
            rows = await self._conn.fetch(
                """
                SELECT doc_id, summary, summary_embedding_ref, sha256, created_at
                  FROM documents
                 WHERE tenant_id = $1
                   AND source_channel = $2
                   AND thread_id = $3
                   AND created_at > now() - make_interval(secs => $4)
                   AND deleted_at IS NULL
                 ORDER BY created_at DESC LIMIT $5
                """,
                tenant_id, source_channel, thread_id, within_seconds, limit,
            )
        else:
            rows = await self._conn.fetch(
                """
                SELECT doc_id, summary, summary_embedding_ref, sha256, created_at
                  FROM documents
                 WHERE tenant_id = $1
                   AND source_channel = $2
                   AND created_at > now() - make_interval(secs => $3)
                   AND deleted_at IS NULL
                 ORDER BY created_at DESC LIMIT $4
                """,
                tenant_id, source_channel, within_seconds, limit,
            )
        return [dict(r) for r in rows]

    async def mark_failed(self, tenant_id: UUID, doc_id: UUID) -> None:
        await self._conn.execute(
            "UPDATE documents SET status = 'failed', updated_at = now() "
            "WHERE tenant_id = $1 AND doc_id = $2",
            tenant_id, doc_id,
        )

    async def touch_access(self, tenant_id: UUID, doc_id: UUID) -> None:
        """Increment access counters. Called by retrieval pipeline (out of scope here, but
        the column lives in ingestion's schema so we expose the helper now)."""
        await self._conn.execute(
            "UPDATE documents "
            "   SET access_count = access_count + 1, last_accessed_at = now() "
            " WHERE tenant_id = $1 AND doc_id = $2",
            tenant_id, doc_id,
        )
