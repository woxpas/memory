"""Ingress stage — sync portion of ingestion (runs inside the API request).

Latency budget: p99 ≤ 200 ms. To meet that we keep this path tight:
  1. Compute SHA-256 (in-memory; large-file streaming hash is a v0.2 task).
  2. Dedup by sha256 — short-circuit returns the existing doc_id.
  3. Encrypt + put bytes to object store (raw bucket).
  4. Insert documents row (status=received) inside a transaction.
  5. Emit ingest.received via OutboxRepo in the SAME transaction (transactional outbox).
  6. Auto-assign suggested scopes (slug lookups; create-on-miss is the agent's job, not ours).
  7. Return doc_id.

Async stages (AV, detect, extract, summarize, chunk, embed, index) run later
via the OutboxPublisher.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from woxpas_adapters import IngestPayload
from woxpas_core import PROFILE_VERSIONS, ProcessingProfile
from woxpas_storage import (
    DocScopeRepo,
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
    ScopeRepo,
    SourceRepo,
    TimelineRepo,
)
from woxpas_storage.objstore import sha256_bytes, sha256_hex

from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_ingest_received

log = structlog.get_logger(__name__)


class DuplicateDocument(Exception):
    """Raised when the same content (by sha256) was already ingested for this tenant."""
    def __init__(self, doc_id: UUID) -> None:
        super().__init__(f"duplicate document: {doc_id}")
        self.doc_id = doc_id


class IngressStage:
    """The synchronous front of the pipeline.

    Constructor takes the long-lived PipelineContext; submit() takes the
    per-request payload. No state on the instance beyond config.
    """

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx

    async def submit(
        self,
        payload: IngestPayload,
        *,
        on_duplicate: str = "return_existing",   # return_existing | reject | force_new
    ) -> tuple[UUID, bool]:
        """Process an inbound ingest payload.

        Returns (doc_id, was_new). For duplicates with `return_existing`, returns
        the prior doc_id with was_new=False — adapter / API can decide what to do.

        Raises DuplicateDocument if on_duplicate='reject' and a match exists.
        """
        env = payload.envelope
        content = payload.content

        # 1. Hash up-front.
        digest_bytes = sha256_bytes(content)
        digest_hex   = sha256_hex(content)

        # 2. Dedup short-circuit (read outside the write tx so we don't hold locks).
        async with self._ctx.pool.tenant_session(env.tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            existing = await doc_repo.by_sha256(env.tenant_id, digest_bytes)
            if existing is not None:
                if on_duplicate == "reject":
                    raise DuplicateDocument(existing["doc_id"])
                if on_duplicate == "return_existing":
                    log.info("ingress.dedup_hit",
                             doc_id=str(existing["doc_id"]),
                             tenant_id=str(env.tenant_id),
                             sha256=digest_hex)
                    # Touch decay signals so it counts as recently re-encountered.
                    await doc_repo.touch_access(env.tenant_id, existing["doc_id"])
                    return existing["doc_id"], False
                # force_new falls through → store as a new row even if same hash.

        # 3. Encrypt + put bytes (outside the write tx; idempotent by content hash).
        dek = await self._ctx.session_dek(env.tenant_id)
        raw_uri, _, raw_iv = await self._ctx.objstore.put_raw_bytes(
            content, dek, content_type=env.mime,
        )

        # 4 + 5. State change + transactional outbox in ONE transaction.
        profile = self._resolve_profile(env.suggested_processing_profile)
        profile_ver = PROFILE_VERSIONS.get(profile.value, "0.1.0")
        pipeline_ver = self._ctx.pipeline.pipeline_version

        async with self._ctx.pool.tenant_session(env.tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            outbox   = OutboxRepo(conn)
            timeline = TimelineRepo(conn)
            scope_repo = ScopeRepo(conn)
            doc_scope  = DocScopeRepo(conn)
            run_repo   = PipelineRunRepo(conn)
            source_repo = SourceRepo(conn)

            # Ensure the source row exists. For agent/API uploads the API may have
            # ensured this already; idempotent get_or_create is cheap.
            await source_repo.get(env.tenant_id, env.source_id)

            # Thread auto-detection: if no explicit thread_hint, look for a recent
            # doc from the same source_channel + captured_by within the configured
            # window. If found, inherit (or seed) its thread_id so a burst of
            # captures clusters together.
            resolved_thread_id = env.thread_hint
            if resolved_thread_id is None:
                resolved_thread_id = await self._auto_thread(
                    conn, env.tenant_id, env.source_channel.value,
                    env.captured_by.value,
                )

            doc_id = await doc_repo.create(
                tenant_id=env.tenant_id,
                source_id=env.source_id,
                source_channel=env.source_channel.value,
                captured_by=env.captured_by.value,
                raw_uri=raw_uri, raw_iv=raw_iv,
                sha256=digest_bytes,
                mime=env.mime,
                size_bytes=len(content),
                filename=env.filename,
                suggested_title=env.suggested_title,
                processing_profile=profile,
                processing_profile_version=profile_ver,
                pipeline_version=pipeline_ver,
                thread_id=resolved_thread_id,
                valid_time_hint=env.valid_time_hint,
                external_refs=[r.model_dump(mode="json") for r in env.external_refs],
                sensitivity_class=env.sensitivity_class,
            )

            # Run log: ingress succeeded.
            run_id = await run_repo.start(
                env.tenant_id, doc_id, "ingress", pipeline_ver, attempt=1,
            )
            await run_repo.succeed(run_id, metrics={
                "size_bytes": len(content), "sha256_hex": digest_hex,
            })

            # Auto-assign suggested scopes (slug-only — no scope creation here;
            # the agent is the only thing that creates scopes after dedup check).
            for slug in env.suggested_scopes:
                scope = await scope_repo.by_slug(env.tenant_id, slug)
                if scope is None:
                    log.warning("ingress.scope_slug_unknown", slug=slug,
                                tenant_id=str(env.tenant_id))
                    continue
                if await doc_scope.assign(
                    env.tenant_id, doc_id, scope["scope_id"],
                    assigned_by="adapter", auto_assigned=True,
                ):
                    await scope_repo.member_count_delta(
                        env.tenant_id, scope["scope_id"], +1,
                    )

            # Timeline event (drives surfacing).
            await timeline.record(
                env.tenant_id, "doc.ingested",
                doc_id=doc_id,
                actor=env.captured_by.value,
                payload={"source_channel": env.source_channel.value},
            )

            # Outbox event — same tx as state change.
            await publish_ingest_received(
                outbox,
                tenant_id=env.tenant_id, doc_id=doc_id,
                pipeline_version=pipeline_ver,
                source_channel=env.source_channel.value,
                sha256_hex=digest_hex,
                mime=env.mime,
                correlation_id=env.correlation_id,
            )

        log.info("ingress.accepted",
                 doc_id=str(doc_id),
                 tenant_id=str(env.tenant_id),
                 channel=env.source_channel.value,
                 size=len(content), profile=profile.value)
        return doc_id, True

    async def _auto_thread(
        self, conn, tenant_id: UUID, source_channel: str, captured_by: str,
    ) -> UUID | None:
        """Find a recent doc from the same channel + author within the thread window.
        If it has a thread_id, inherit it. If it has none, mint a new thread_id and
        backfill the recent doc, returning the same id. Returns None when no peer found.
        """
        from uuid import uuid4
        window = self._ctx.pipeline.thread_window_seconds
        if window <= 0:
            return None
        row = await conn.fetchrow(
            """
            SELECT doc_id, thread_id
              FROM documents
             WHERE tenant_id = $1
               AND source_channel = $2
               AND captured_by = $3
               AND deleted_at IS NULL
               AND created_at > now() - make_interval(secs => $4)
             ORDER BY created_at DESC
             LIMIT 1
            """,
            tenant_id, source_channel, captured_by, window,
        )
        if row is None:
            return None
        if row["thread_id"] is not None:
            return row["thread_id"]
        # Peer has no thread yet — seed a new one and backfill it on the peer.
        new_thread = uuid4()
        await conn.execute(
            "UPDATE documents SET thread_id = $3, updated_at = now() "
            "WHERE tenant_id = $1 AND doc_id = $2 AND thread_id IS NULL",
            tenant_id, row["doc_id"], new_thread,
        )
        log.info("ingress.thread_seeded",
                 tenant_id=str(tenant_id),
                 thread_id=str(new_thread),
                 peer_doc_id=str(row["doc_id"]))
        return new_thread

    @staticmethod
    def _resolve_profile(suggested: str | None) -> ProcessingProfile:
        if not suggested:
            return ProcessingProfile.PERSONAL
        try:
            return ProcessingProfile(suggested)
        except ValueError:
            log.warning("ingress.unknown_profile_suggestion", suggested=suggested)
            return ProcessingProfile.PERSONAL
