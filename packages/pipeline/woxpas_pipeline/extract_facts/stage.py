"""ExtractFactsStage — Phase 2.1.

Subscribes to doc.indexed. For each chunk in the doc, runs a profile-aware
extraction LLM call and persists structured rows to entities, ideas,
tasks_deadlines, relations, with chunk_id linkage. Emits doc.extracted_facts.

Architecture decisions:
  - Per-chunk extraction: chunks are non-overlapping (pysbd-aware sentence
    boundaries from the chunk stage), each ≤ 1024 tokens, so we get full
    LLM attention with no silent content loss to context window caps.
  - Sequential within doc: llama-cpp serializes on a single Llama instance
    anyway, so per-chunk calls can't parallelize. Across docs, pipeline
    concurrency settings still apply.
  - Cloud-policy gate fires ONCE per doc (profile + sensitivity_class don't
    change mid-doc). Audit emits per-chunk on cloud backends so token cost
    is fully accounted.
  - Best-effort merge: same entity name within doc → one entity_id (via
    EntityRepo.get_or_create_by_name; case-insensitive). Claims and ideas
    can have minor duplication across chunks — that's a feature (provenance
    per chunk). Phase 2.2 linking stage refines via embedding cosine.
  - "me" / "I" / "my" → tenant.self_entity_id at persistence time.

Failure modes (per-chunk, non-fatal):
  - LLM error on a single chunk → log + skip that chunk; continue with the rest
  - JSON parse failure → empty arrays from parser; skip that chunk's persist
  - DB error on a single row → log + continue (per-row try/except)
Doc-level failure (entire stage fails):
  - Cloud-policy denial → record_failure, doc-level failure event, no retry
  - All chunks errored → record_failure with retriable=true
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from woxpas_core import Event, EventType
from woxpas_storage import (
    ChunkRepo,
    DocumentRepo,
    EntityRepo,
    IdeaRepo,
    OutboxRepo,
    PipelineRunRepo,
    RelationRepo,
    TaskRepo,
)

from woxpas_pipeline.cloud_policy import (
    CloudPolicyDenied,
    assert_cloud_allowed,
    audit_cloud_egress,
)
from woxpas_pipeline.context import PipelineContext
from woxpas_pipeline.events import publish_doc_extracted_facts, publish_doc_failed
from woxpas_pipeline.extract_facts.prompt import (
    EXTRACTION_PROFILE_VERSION,
    ExtractionResult,
    build_messages,
    parse_extraction,
)

log = structlog.get_logger(__name__)


# Soft cap on chunks per doc — beyond this we mark `extraction_truncated`.
# A 200-page contract that explodes into 300+ chunks shouldn't pin the
# worker for half an hour. Pillar 1 (speed) safety knob.
_DEFAULT_MAX_CHUNKS = 50

# Owner-name aliases that resolve to the tenant's self_entity_id.
_SELF_ALIASES = frozenset({"me", "i", "my", "myself"})


class ExtractFactsStage:
    name = "extract_facts"

    def __init__(
        self,
        ctx: PipelineContext,
        max_chunks_per_doc: int = _DEFAULT_MAX_CHUNKS,
    ) -> None:
        self._ctx = ctx
        self._sem = asyncio.Semaphore(ctx.concurrency("summarize"))   # share LLM budget
        self._max_chunks = max_chunks_per_doc

    def event_type(self) -> EventType:
        return EventType.DOC_INDEXED

    async def handle(self, event: Event) -> None:
        async with self._sem:
            await self._handle(event)

    async def _handle(self, event: Event) -> None:
        tenant_id: UUID = event.tenant_id
        doc_id:    UUID = event.aggregate_id
        pv             = self._ctx.pipeline.pipeline_version

        # 1. Load doc + chunks under tenant RLS.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            doc = await DocumentRepo(conn).get(tenant_id, doc_id)
            if doc is None:
                log.warning("extract_facts.doc_missing", doc_id=str(doc_id))
                return
            run_id = await PipelineRunRepo(conn).start(
                tenant_id, doc_id, self.name, pv,
            )
            chunks = await ChunkRepo(conn).by_doc(tenant_id, doc_id)
            self_entity_id = await conn.fetchval(
                "SELECT self_entity_id FROM tenants WHERE tenant_id = $1",
                tenant_id,
            )

        if not chunks:
            log.info("extract_facts.no_chunks_skip", doc_id=str(doc_id))
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                await PipelineRunRepo(conn).succeed(run_id, metrics={
                    "skipped": "no_chunks",
                })
            return

        truncated = False
        if len(chunks) > self._max_chunks:
            log.warning("extract_facts.truncating",
                        doc_id=str(doc_id), total=len(chunks),
                        max=self._max_chunks)
            chunks = chunks[:self._max_chunks]
            truncated = True

        profile     = doc["processing_profile"]
        sensitivity = doc.get("sensitivity_class") or "standard"
        title       = doc.get("title") or doc.get("filename")

        # 2. Cloud-policy gate fires once per doc.
        try:
            backend = await self._ctx.llm_backend("extraction")
            await assert_cloud_allowed(
                self._ctx.pool,
                tenant_id=tenant_id,
                processing_profile=profile,
                sensitivity_class=sensitivity,
                backend=backend,
            )
        except CloudPolicyDenied as e:
            log.warning("extract_facts.cloud_denied",
                        doc_id=str(doc_id), reason=str(e), gate=e.gate)
            await self._record_failure(
                tenant_id, doc_id, run_id,
                "cloud_policy_denied", str(e),
                retriable=False, causation_id=event.correlation_id,
            )
            return

        # 3. Per-chunk extraction loop. State accumulates across chunks.
        started = time.monotonic()
        chunks_processed = 0
        chunks_failed    = 0
        entities_count   = 0
        ideas_count      = 0
        tasks_count      = 0
        claims_count     = 0

        for chunk in chunks:
            chunk_id   = chunk["chunk_id"]
            chunk_text = chunk.get("text") or ""
            if not chunk_text.strip():
                continue

            try:
                result = await self._extract_chunk(
                    backend=backend,
                    chunk_text=chunk_text,
                    processing_profile=profile,
                    sensitivity_class=sensitivity,
                    doc_title=title,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    tenant_id=tenant_id,
                )
            except Exception as e:
                chunks_failed += 1
                log.warning("extract_facts.chunk_failed",
                            doc_id=str(doc_id), chunk_id=str(chunk_id),
                            error=str(e), error_class=type(e).__name__)
                continue

            try:
                counts = await self._persist_chunk_extractions(
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    self_entity_id=self_entity_id,
                    result=result,
                )
            except Exception as e:
                chunks_failed += 1
                log.warning("extract_facts.persist_failed",
                            doc_id=str(doc_id), chunk_id=str(chunk_id),
                            error=str(e), error_class=type(e).__name__)
                continue

            chunks_processed += 1
            entities_count += counts["entities"]
            ideas_count    += counts["ideas"]
            tasks_count    += counts["tasks"]
            claims_count   += counts["claims"]

        duration_ms = int((time.monotonic() - started) * 1000)

        # 4. If every chunk failed, mark the run failed (retriable).
        if chunks_processed == 0 and chunks_failed > 0:
            await self._record_failure(
                tenant_id, doc_id, run_id,
                "all_chunks_failed",
                f"all {chunks_failed} chunks failed extraction",
                retriable=True, causation_id=event.correlation_id,
            )
            return

        # 5. Persist run success + emit event in one transaction.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            outbox   = OutboxRepo(conn)
            run_repo = PipelineRunRepo(conn)
            await run_repo.succeed(run_id, metrics={
                "chunks_processed":  chunks_processed,
                "chunks_failed":     chunks_failed,
                "chunks_truncated":  truncated,
                "entities_count":    entities_count,
                "ideas_count":       ideas_count,
                "tasks_count":       tasks_count,
                "claims_count":      claims_count,
                "extractor_version": EXTRACTION_PROFILE_VERSION,
                "model_version":     backend.model_version,
                "duration_ms":       duration_ms,
            })
            await publish_doc_extracted_facts(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id, pipeline_version=pv,
                chunks_processed=chunks_processed,
                chunks_failed=chunks_failed,
                entities_count=entities_count,
                ideas_count=ideas_count,
                tasks_count=tasks_count,
                claims_count=claims_count,
                extractor_version=EXTRACTION_PROFILE_VERSION,
                causation_id=event.correlation_id,
            )

        log.info("extract_facts.ok",
                 doc_id=str(doc_id),
                 chunks_processed=chunks_processed, chunks_failed=chunks_failed,
                 entities=entities_count, ideas=ideas_count,
                 tasks=tasks_count, claims=claims_count,
                 duration_ms=duration_ms, model=backend.model_version)

    # ─── Per-chunk LLM call ────────────────────────────────────────────────

    async def _extract_chunk(
        self,
        *,
        backend,
        chunk_text:         str,
        processing_profile: str,
        sensitivity_class:  str,
        doc_title:          str | None,
        doc_id:             UUID,
        chunk_id:           UUID,
        tenant_id:          UUID,
    ) -> ExtractionResult:
        messages = build_messages(
            chunk_text,
            processing_profile=processing_profile,
            doc_title=doc_title,
            doc_id=doc_id,
            chunk_id=chunk_id,
        )
        try:
            response = await backend.generate(
                messages,
                # Output budget tuned for typical chunks: entities + claims +
                # ideas + tasks rarely exceed 500-700 tokens of JSON. 800
                # leaves headroom; pushing higher costs CPU latency without
                # accuracy gain (we'd just be giving the model rope to ramble).
                max_tokens=800,
                temperature=0.0,
                response_format="json_object",
                # Qwen-3B on CPU (Intel Mac, no Metal/CUDA) generates ~5-10
                # tokens/sec, so 800 tokens worst case = ~160s. 300s gives
                # margin for slow boxes; lowers when we ship GPU defaults.
                timeout_s=300.0,
            )
        except Exception as call_exc:
            await audit_cloud_egress(
                self._ctx.pool,
                tenant_id=tenant_id,
                processing_profile=processing_profile,
                sensitivity_class=sensitivity_class,
                backend=backend, usage=None,
                outcome="error", error_reason=type(call_exc).__name__,
            )
            raise
        await audit_cloud_egress(
            self._ctx.pool,
            tenant_id=tenant_id,
            processing_profile=processing_profile,
            sensitivity_class=sensitivity_class,
            backend=backend, usage=response.usage,
            outcome="success",
        )
        return parse_extraction(response.text)

    # ─── Persist one chunk's extractions ───────────────────────────────────

    async def _persist_chunk_extractions(
        self,
        *,
        tenant_id:      UUID,
        doc_id:         UUID,
        chunk_id:       UUID,
        self_entity_id: UUID | None,
        result:         ExtractionResult,
    ) -> dict[str, int]:
        """Resolve names → entity_ids via get_or_create, then insert ideas /
        tasks / claims with chunk_id linkage. Per-row try/except so one bad
        row doesn't lose the whole chunk."""
        # Build a per-chunk name → entity_id map. Pre-create all entity rows
        # first so subsequent ideas/tasks/claims can reference them.
        name_to_id: dict[str, UUID] = {}

        # Each row is wrapped in its own savepoint so a single CHECK-constraint
        # violation or transient error doesn't poison the outer transaction
        # ("current transaction is aborted, commands ignored ..."). Savepoint
        # rollback isolates the failure to that one row; the rest persist.
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            entity_repo = EntityRepo(conn)
            for ent in result.entities:
                key = ent.name.strip().lower()
                if not key or key in name_to_id:
                    continue
                if key in _SELF_ALIASES and self_entity_id is not None:
                    name_to_id[key] = self_entity_id
                    try:
                        async with conn.transaction():
                            await entity_repo.bump_mention(tenant_id, self_entity_id)
                    except Exception as e:
                        log.warning("extract_facts.entity_persist_failed",
                                    name=ent.name, error=str(e))
                    continue
                try:
                    async with conn.transaction():
                        eid = await entity_repo.get_or_create_by_name(
                            tenant_id,
                            canonical_name=ent.name.strip(),
                            entity_type=ent.type,
                        )
                        await entity_repo.bump_mention(tenant_id, eid)
                    name_to_id[key] = eid
                except Exception as e:
                    log.warning("extract_facts.entity_persist_failed",
                                name=ent.name, error=str(e))

        async def _resolve(name: str | None) -> UUID | None:
            if not name:
                return self_entity_id        # absent owner = self by convention
            key = name.strip().lower()
            if key in _SELF_ALIASES:
                return self_entity_id
            if key in name_to_id:
                return name_to_id[key]
            # Late-binding lookup for names that appeared in claims/owners
            # but not in the entities array. Permissive type='concept'.
            async with self._ctx.pool.tenant_session(tenant_id) as conn:
                eid = await EntityRepo(conn).get_or_create_by_name(
                    tenant_id,
                    canonical_name=name.strip(),
                    entity_type="concept",
                )
            name_to_id[key] = eid
            return eid

        idea_count = task_count = claim_count = 0

        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            idea_repo  = IdeaRepo(conn)
            task_repo  = TaskRepo(conn)
            rel_repo   = RelationRepo(conn)

            for idea in result.ideas:
                try:
                    owner_id = await _resolve(idea.owner)
                    async with conn.transaction():
                        await idea_repo.insert(
                            tenant_id,
                            doc_id=doc_id,
                            title=idea.title,
                            summary=idea.summary,
                            category=idea.category,
                            owner_entity_id=owner_id,
                            extractor_version=EXTRACTION_PROFILE_VERSION,
                        )
                    idea_count += 1
                except Exception as e:
                    log.warning("extract_facts.idea_persist_failed",
                                title=idea.title, error=str(e))

            for task in result.tasks:
                try:
                    owner_id = await _resolve(task.owner)
                    due_at   = _safe_parse_iso(task.due_iso)
                    async with conn.transaction():
                        await task_repo.insert(
                            tenant_id,
                            doc_id=doc_id,
                            title=task.title,
                            description=task.description,
                            kind=task.kind,
                            due_at=due_at,
                            all_day=(due_at is not None and len(task.due_iso or "") <= 10),
                            owner_entity_id=owner_id,
                            extractor_version=EXTRACTION_PROFILE_VERSION,
                        )
                    task_count += 1
                except Exception as e:
                    log.warning("extract_facts.task_persist_failed",
                                title=task.title, error=str(e))

            for claim in result.claims:
                try:
                    subj_id = await _resolve(claim.subject)
                    if subj_id is None:
                        continue
                    if isinstance(claim.object, dict):
                        obj_value: dict[str, Any] | None = claim.object
                        obj_id: UUID | None = None
                    else:
                        # Treat object as a name first; fall back to literal value.
                        obj_id = await _resolve(claim.object)
                        if obj_id is not None:
                            obj_value = None
                        else:
                            obj_value = {"text": claim.object}
                    async with conn.transaction():
                        # claim.kind is the LLM's semantic-kind hint
                        # (attribute|relation|temporal|...). NOT persisted —
                        # linking stage re-derives this from the predicates
                        # registry. Captured here so it shows up in worker
                        # logs for debugging prompt quality.
                        await rel_repo.insert(
                            tenant_id,
                            subject_entity_id=subj_id,
                            predicate=claim.predicate,
                            object_entity_id=obj_id,
                            object_value=obj_value,
                            claim_text=claim.claim_text,
                            doc_id=doc_id,
                            extractor_version=EXTRACTION_PROFILE_VERSION,
                        )
                    log.debug("extract_facts.claim_persisted",
                              subject=claim.subject, predicate=claim.predicate,
                              llm_kind=claim.kind)
                    claim_count += 1
                except Exception as e:
                    log.warning("extract_facts.claim_persist_failed",
                                subject=claim.subject,
                                predicate=claim.predicate,
                                error=str(e))

        return {
            "entities": len(name_to_id),
            "ideas":    idea_count,
            "tasks":    task_count,
            "claims":   claim_count,
        }

    # ─── Failure path ──────────────────────────────────────────────────────

    async def _record_failure(
        self,
        tenant_id: UUID, doc_id: UUID, run_id: UUID,
        error_class: str, error_message: str, retriable: bool,
        causation_id: UUID | None,
    ) -> None:
        async with self._ctx.pool.tenant_session(tenant_id) as conn:
            run_repo = PipelineRunRepo(conn)
            outbox   = OutboxRepo(conn)
            await run_repo.fail(run_id, error_class, error_message, retriable=retriable)
            await publish_doc_failed(
                outbox,
                tenant_id=tenant_id, doc_id=doc_id,
                pipeline_version=self._ctx.pipeline.pipeline_version,
                stage=self.name,
                error_class=error_class, error_message=error_message,
                retriable=retriable,
                causation_id=causation_id,
            )


def _safe_parse_iso(due_iso: str | None) -> datetime | None:
    """Best-effort ISO 8601 parse. Returns None on bad/missing input rather
    than raising — extraction's stake in date correctness is low; users
    edit deadlines downstream."""
    if not due_iso:
        return None
    s = due_iso.strip()
    if not s or s.lower() in {"null", "none"}:
        return None
    # Plain date (YYYY-MM-DD) — promote to midnight UTC.
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s + "T00:00:00+00:00")
        # Datetime with optional Z → +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None
