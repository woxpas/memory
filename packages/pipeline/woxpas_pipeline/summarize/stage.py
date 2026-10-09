"""Summarize stage — produces document.summary + topics_inferred.

Subscribes to doc.normalized. Reads canonical_blocks from Postgres (no objstore
round-trip needed; the CDM is fully reproduced in the table). Sends concatenated
body text through the local LLM, writes results back to documents row, emits
doc.summarized.

This stage also embeds the summary and upserts it into the Qdrant
summaries_collection (the tier-zero index retrieval narrows by before scanning
chunks). Embedding failures are non-fatal — logged and retried on next ingest.

Concurrency: bounded by an asyncio.Semaphore (default 1 — the LLM is the
bottleneck on CPU, parallel runs would just thrash). Tune via settings.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import structlog
from woxpas_core import Event, EventType
from woxpas_core.ids import content_uuid
from woxpas_storage import (
    DocumentRepo,
    OutboxRepo,
    PipelineRunRepo,
    SummaryPoint,
    TimelineRepo,
)

from woxpas_pipeline.cloud_policy import (
    CloudPolicyDenied,
    assert_cloud_allowed,
    audit_cloud_egress,
)
from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_failed, publish_doc_summarized
from woxpas_pipeline.summarize.prompt import (
    build_messages,
    parse_summary,
    tiny_doc_summary,
)

log = structlog.get_logger(__name__)


_BODY_BLOCK_TYPES_PRIORITY = (
    # Concat order — heading first so the model sees structural context.
    "heading", "paragraph", "list_item", "quote", "caption", "table_row",
    "code", "math", "footnote", "audio_segment", "image_caption",
)


def _assemble_body(blocks: list[dict[str, Any]], cap_chars: int = 24000) -> str:
    """Concatenate block.text in document order, capped to summarizer context budget."""
    parts: list[str] = []
    total = 0
    for b in blocks:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        # Light heading marker so the model doesn't lose hierarchy.
        if b["type"] == "heading":
            level = b.get("level") or 1
            text = ("#" * min(level, 6)) + " " + text
        chunk = text + "\n"
        if total + len(chunk) > cap_chars:
            parts.append(chunk[:cap_chars - total])
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts).strip()


class SummarizeStage:
    name = "summarize"

    def __init__(self, ctx: PipelineContext) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("summarize"))

    def event_type(self) -> EventType:
        return EventType.DOC_NORMALIZED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        doc_id    = event.aggregate_id
        pv        = self._ctx.pipeline.pipeline_version

        # Open run + load blocks under tenant RLS.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            run_repo = PipelineRunRepo(conn)
            doc = await doc_repo.get(tenant_id, doc_id)
            if doc is None:
                log.warning("summarize.doc_missing", doc_id=str(doc_id))
                return

            run_id = await run_repo.start(tenant_id, doc_id, self.name, pv)

            blocks = await conn.fetch(
                """
                SELECT type, level, text
                  FROM canonical_blocks
                 WHERE tenant_id = $1 AND doc_id = $2
                 ORDER BY ordinal
                """,
                tenant_id, doc_id,
            )

        body = _assemble_body([dict(b) for b in blocks])
        if not body:
            log.warning("summarize.empty_body", doc_id=str(doc_id))
            body = doc.get("title") or doc.get("filename") or "(empty document)"

        # Run LLM (or tiny-doc shortcut). This is the slow bit on CPU.
        # Tiny-doc shortcut runs first so we don't even build/dispatch a backend
        # for a one-liner. For real LLM calls: pick backend → cloud-policy gate
        # (raises CloudPolicyDenied if a personal-data profile would egress with
        # policy off) → call → audit.
        try:
            if len(body) < 200:
                result = tiny_doc_summary(body)
            else:
                backend     = await self._ctx.llm_backend("summary")
                profile     = doc["processing_profile"]
                sensitivity = doc.get("sensitivity_class") or "standard"
                await assert_cloud_allowed(
                    self._ctx.pool,
                    tenant_id=tenant_id,
                    processing_profile=profile,
                    sensitivity_class=sensitivity,
                    backend=backend,
                )
                messages = build_messages(body, doc_id=doc_id)
                try:
                    response = await backend.generate(
                        messages, max_tokens=256, temperature=0.0,
                        response_format="json_object",
                    )
                except Exception as call_exc:
                    await audit_cloud_egress(
                        self._ctx.pool,
                        tenant_id=tenant_id, processing_profile=profile,
                        sensitivity_class=sensitivity,
                        backend=backend, usage=None,
                        outcome="error", error_reason=type(call_exc).__name__,
                    )
                    raise
                await audit_cloud_egress(
                    self._ctx.pool,
                    tenant_id=tenant_id, processing_profile=profile,
                    sensitivity_class=sensitivity,
                    backend=backend, usage=response.usage,
                    outcome="success",
                )
                result = parse_summary(response.text, model_version=backend.model_version)
        except CloudPolicyDenied as e:
            log.warning("summarize.cloud_denied", doc_id=str(doc_id), reason=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "cloud_policy_denied", str(e),
                retriable=False, causation_id=event.correlation_id,
            )
            return
        except Exception as e:
            log.error("summarize.llm_failed", doc_id=str(doc_id), error=str(e))
            await self._record_failure(
                tenant_id, doc_id, run_id, "llm_error", str(e),
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # Tier 2 sensitivity upgrade — applied as soon as we have the summary
        # back, BEFORE we publish doc.summarized. Subsequent stages (Tier 3
        # refine, extraction) will see the upgraded flag immediately.
        # Monotonic up-only: if Tier 1 (manual) or 3a (heuristic) already
        # marked sensitive, this no-ops. The reason is appended.
        if result.is_sensitive:
            llm_reason = (
                f"llm:{result.sensitivity_reason or 'flagged_by_summarizer'}"
            )
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                await DocumentRepo(conn).set_sensitivity_sensitive(
                    tenant_id, doc_id, llm_reason,
                )
            log.info("summarize.sensitivity_upgraded",
                     doc_id=str(doc_id), tier="2",
                     reason=result.sensitivity_reason)

        # Embed the summary text (dense only) for topic-level semantic search
        # across documents — the only use case we accepted from the summary-
        # embedding case. Sparse + ColBERT live with chunks; this is doc-level.
        embedding_ref: UUID | None = None
        dense: list[float] | None = None
        try:
            embedder = await self._ctx.embedder()
            batch = await embedder.embed([result.summary], batch_size=1)
            dense = batch.dense[0] if batch.dense else None
            if dense is not None:
                embedding_ref = content_uuid("summary", str(doc_id), result.model_version)
                # Look up scope_ids for this doc so the Qdrant payload mirrors
                # what filter pushdown will need at retrieval time.
                async with self._ctx.pool.tenant_session(tenant_id) as conn:
                    rows = await conn.fetch(
                        "SELECT scope_id FROM doc_scopes "
                        "WHERE tenant_id = $1 AND doc_id = $2",
                        tenant_id, doc_id,
                    )
                scope_ids = [r["scope_id"] for r in rows]
                await self._ctx.qdrant.upsert_summaries([SummaryPoint(
                    point_id=embedding_ref,
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    scope_ids=scope_ids,
                    processing_profile=doc["processing_profile"],
                    topics=result.topics,
                    dense=dense,
                )])
        except Exception as e:
            # Embedding failure is non-fatal — we still want the summary text persisted.
            # Log and continue; the embedder will retry on next ingest of this doc.
            log.warning("summarize.embed_failed",
                        doc_id=str(doc_id), error=str(e))
            embedding_ref = None
            dense = None

        # Scope auto-assignment — match summary embedding to existing scopes.
        # Skipped if (a) the doc was already manually scoped at ingest, (b) embedding
        # failed, or (c) feature disabled in settings. Threshold-gated; on match we
        # set auto_assigned=true so the user / agent can override or unassign later.
        if (
            self._ctx.pipeline.scope_assign_enabled
            and dense is not None
            and embedding_ref is not None
        ):
            # Side-effecting: assigns the scope in the DB. The returned match
            # detail is not consumed here.
            await self._auto_assign_scope(
                tenant_id, doc_id, dense,
                captured_by=doc.get("captured_by"),
            )

        # Persist + emit in one transaction.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc_repo = DocumentRepo(conn)
            outbox   = OutboxRepo(conn)
            timeline = TimelineRepo(conn)
            run_repo = PipelineRunRepo(conn)

            await doc_repo.update_summary(
                tenant_id, doc_id,
                summary=result.summary,
                model_version=result.model_version,
                profile_version=result.profile_version,
                topics=result.topics,
                summary_embedding_ref=embedding_ref,
            )
            await run_repo.succeed(run_id, metrics={
                "model": result.model_version,
                "topics_count": len(result.topics),
                "body_chars": len(body),
            })
            await timeline.record(
                tenant_id, "doc.summarized",
                doc_id=doc_id, actor="system",
                payload={
                    "model_version": result.model_version,
                    "topics": result.topics,
                },
            )
            await publish_doc_summarized(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=pv,
                summary_preview=result.summary,
                topics=result.topics,
                model_version=result.model_version,
                profile_version=result.profile_version,
                causation_id=event.correlation_id,
            )

        log.info("summarize.ok",
                 doc_id=str(doc_id),
                 model=result.model_version,
                 chars=len(body),
                 summary_len=len(result.summary),
                 topics=len(result.topics))

    async def _auto_assign_scope(
        self, tenant_id: UUID, doc_id: UUID, query_vec: list[float],
        *, captured_by: str | None = None,
    ) -> dict[str, Any] | None:
        """Match summary embedding against existing scopes; assign top match
        if cosine > threshold. Returns the assigned scope dict on success,
        None on miss / no-op. Non-fatal if anything raises.

        Phase 5a.3.4 Track 2a: when `captured_by='agent'` and the normal
        threshold doesn't match, retry IMMEDIATELY at a lower threshold
        (matches ScopeReassigner's). Rationale: the agent had conversation
        context when ingesting; the doc almost certainly belongs to one
        of the user's existing projects. No reason to make it wait for
        the dreamer cycle.
        """
        threshold = self._ctx.pipeline.scope_assign_threshold
        try:
            candidates = await self._ctx.qdrant.search_scopes_by_vector(
                tenant_id, query_vec, limit=3, prefer="centroid",
            )
        except Exception as e:
            log.warning("summarize.scope_search_failed",
                        doc_id=str(doc_id), error=str(e))
            return None

        if not candidates:
            return None
        top = candidates[0]
        if top["score"] < threshold or not top.get("scope_id"):
            # Phase 5a.3.4 Track 2a: agent-captured second pass
            agent_threshold = self._ctx.pipeline.scope_assign_threshold_agent
            if (
                captured_by == "agent"
                and top["score"] >= agent_threshold
                and top.get("scope_id")
            ):
                log.info("summarize.scope_agent_second_pass",
                         doc_id=str(doc_id),
                         score=round(top["score"], 4),
                         normal_threshold=threshold,
                         agent_threshold=agent_threshold)
                # fall through using `top` at the agent threshold
            else:
                log.info("summarize.scope_no_match",
                         doc_id=str(doc_id),
                         best_score=round(top["score"], 4),
                         threshold=threshold,
                         captured_by=captured_by)
                return None

        scope_id = UUID(top["scope_id"])
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            from woxpas_storage import DocScopeRepo, ScopeRepo, TimelineRepo
            doc_scope = DocScopeRepo(conn)
            scope_repo = ScopeRepo(conn)
            timeline = TimelineRepo(conn)
            inserted = await doc_scope.assign(
                tenant_id, doc_id, scope_id,
                assigned_by="classifier",       # consistent with our schema CHECK enum
                auto_assigned=True,
                confidence=top["score"],
            )
            if inserted:
                await scope_repo.member_count_delta(tenant_id, scope_id, +1)
                await timeline.record(
                    tenant_id, "doc.summarized",
                    doc_id=doc_id, scope_id=scope_id, actor="classifier",
                    payload={
                        "stage": "scope_auto_assign",
                        "score": top["score"],
                        "threshold": threshold,
                    },
                )
                log.info("summarize.scope_auto_assigned",
                         doc_id=str(doc_id),
                         scope_id=str(scope_id),
                         score=round(top["score"], 4))
                return {
                    "scope_id": scope_id,
                    "score": top["score"],
                    "newly_assigned": True,
                }
            else:
                log.info("summarize.scope_already_assigned",
                         doc_id=str(doc_id), scope_id=str(scope_id))
                return {
                    "scope_id": scope_id,
                    "score": top["score"],
                    "newly_assigned": False,
                }

    async def _record_failure(
        self, tenant_id: UUID, doc_id: UUID, run_id: UUID, error_class: str,
        message: str, retriable: bool, causation_id: UUID | None,
    ) -> None:
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_repo = PipelineRunRepo(conn)
            outbox   = OutboxRepo(conn)
            await run_repo.fail(run_id, error_class, message, retriable=retriable)
            await publish_doc_failed(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                stage=self.name, error_class=error_class, error_message=message,
                retriable=retriable, causation_id=causation_id,
            )
