"""WoxpasMcpServer — stdio MCP server, single-tenant per process.

The construct:
  1. Read tenant_id from env (refuse to start if absent).
  2. Build PipelineContext (pool + qdrant + objstore + DEK).
  3. Warm embedder + reranker (so first /recall is fast).
  4. Construct all engines (retrieval, synthesis, resurface, scope_admin,
     document_admin, document_read, conflict, ingress).
  5. Import the tools module to populate the registry decorators.
  6. Spin up the MCP stdio server, bind tools/list + tools/call.

v0.2 forward-design seams:
  - Transport ABC (stdio is one impl; HTTP/SSE another) — the MCP SDK
    already supports both, but our adapter wraps stdio for v0.1.
  - AuthResolver ABC (env-var resolver is one impl; OAuth-token is v0.2)
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from mcp import types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

# Import the tools package so registry decorators fire.
import woxpas_mcp.tools  # noqa: F401
from woxpas_mcp.context import ToolContext
from woxpas_mcp.errors import StructuredError, ToolError
from woxpas_mcp.middleware import AuditWriter, RateLimiter, emit_latency
from woxpas_mcp.registry import all_tools, get_tool
from woxpas_mcp.settings import McpSettings

if TYPE_CHECKING:
    from woxpas_runtime import LlmBackend

log = structlog.get_logger(__name__)


class WoxpasMcpServer:
    """The MCP server. Construct, then call .serve() to enter the
    stdio loop until the parent closes the pipe."""

    def __init__(self, *, settings: McpSettings, ctx: ToolContext) -> None:
        self._settings = settings
        self._ctx      = ctx
        self._mcp = Server("woxpas-mcp")
        # Phase 5a.5.4 hardening — token-bucket rate limiter + audit log
        # + latency instrumentation. Cheap; wraps every tool call.
        self._rate_limiter = RateLimiter(
            rate_per_minute=settings.rate_limit_per_minute,
        )
        self._audit_writer = AuditWriter(
            pool=ctx.pipeline_ctx.pool,
            level=settings.audit_level,
            actor="mcp-agent",        # v0.2: derive from MCP client info handshake
        )
        self._wire_handlers()

    # ─── Public ─────────────────────────────────────────────────────────────

    @classmethod
    async def construct(cls) -> WoxpasMcpServer:
        """Build engines + ToolContext from env. Raises if WOXPAS_MCP_TENANT_ID
        is missing."""
        settings = McpSettings()                                # raises if tenant_id absent
        log.info("mcp.server.constructing",
                 tenant_id=str(settings.tenant_id))

        # Build the same engines as AppState. Lazy embedder + reranker
        # for warm first /recall.
        from woxpas_api.engines.conflict import ConflictEngine
        from woxpas_api.engines.document_admin import DocumentAdminEngine
        from woxpas_api.engines.document_read import DocumentReadEngine
        from woxpas_api.engines.scope_admin import ScopeAdminEngine
        from woxpas_pipeline import IngressStage, PipelineContext
        from woxpas_resurfacing import ResurfaceEngine
        from woxpas_retrieval import RetrievalEngine
        from woxpas_runtime import build_llm_backend
        from woxpas_synthesis import CitationVerifier, SynthesisEngine

        pctx = await PipelineContext.construct()

        # Bootstrap storage — idempotent, creates buckets + collections if missing.
        await pctx.qdrant.bootstrap()
        await pctx.objstore.bootstrap()

        embedder = await pctx.embedder()
        import sys as _sys
        print("  bootstrapping storage…", end=" ", flush=True, file=_sys.stderr)
        print("✓", file=_sys.stderr)
        print("  warming embedder…", end=" ", flush=True, file=_sys.stderr)
        await embedder.embed(["warmup"], batch_size=1)
        print("✓", file=_sys.stderr)
        reranker = await pctx.reranker()
        intent_llm = None
        try:
            intent_llm = await pctx.llm_backend("summary")
        except RuntimeError:
            intent_llm = None

        retrieval = RetrievalEngine(
            pool=pctx.pool, qdrant=pctx.qdrant,
            embedder=embedder, reranker=reranker,
            intent_llm=intent_llm,
        )
        verifier = CitationVerifier(embedder)
        backend_cache: dict[str, LlmBackend] = (
            {intent_llm.name: intent_llm} if intent_llm else {}
        )

        def llm_factory(name: str | None) -> LlmBackend:
            if name is None and intent_llm is not None:
                return intent_llm
            resolved = name or "qwen2.5-3b-instruct-q4km"
            cached = backend_cache.get(resolved)
            if cached is not None:
                return cached
            backend = build_llm_backend(resolved)
            backend_cache[resolved] = backend
            return backend

        synthesis = SynthesisEngine(
            retrieval=retrieval, llm_factory=llm_factory,
            pool=pctx.pool, verifier=verifier,
        )
        resurface = ResurfaceEngine(pool=pctx.pool)
        scope_admin = ScopeAdminEngine(
            pool=pctx.pool, qdrant=pctx.qdrant, embedder=embedder,
        )
        document_admin = DocumentAdminEngine(pool=pctx.pool)
        document_read  = DocumentReadEngine(ctx=pctx)
        conflict       = ConflictEngine(pool=pctx.pool)
        ingress        = IngressStage(pctx)

        ctx = ToolContext(
            tenant_id=settings.tenant_id,
            pipeline_ctx=pctx,
            retrieval=retrieval,
            synthesis=synthesis,
            resurface=resurface,
            scope_admin=scope_admin,
            document_admin=document_admin,
            document_read=document_read,
            conflict=conflict,
            ingress=ingress,
        )
        log.info("mcp.server.constructed",
                 tenant_id=str(settings.tenant_id),
                 tools_registered=len(all_tools()))
        return cls(settings=settings, ctx=ctx)

    async def serve(self) -> None:
        """Enter the stdio loop. Returns when the parent closes the pipe
        or SIGTERM lands."""
        async with stdio_server() as (reader, writer):
            await self._mcp.run(
                reader, writer,
                self._mcp.create_initialization_options(),
            )

    async def close(self) -> None:
        await self._ctx.close()

    # ─── Wiring ────────────────────────────────────────────────────────────

    def _wire_handlers(self) -> None:
        """Bind list_tools + call_tool handlers to the SDK's Server."""

        @self._mcp.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]  # MCP SDK decorator is untyped
        async def _list_tools() -> list[mcp_types.Tool]:
            tools = []
            for t in all_tools():
                tools.append(mcp_types.Tool(
                    name=t.name,
                    description=t.description,
                    inputSchema=t.input_model.model_json_schema(),
                ))
            return tools

        @self._mcp.call_tool()  # type: ignore[untyped-decorator]  # MCP SDK decorator is untyped
        async def _call_tool(
            name: str, arguments: dict[str, Any] | None,
        ) -> list[mcp_types.TextContent]:
            """Hardened call dispatcher.

            Order:
              1. Rate-limit check (token bucket; refuses early on burst)
              2. Tool lookup (not_found if unknown)
              3. Pydantic input validation (validation_failed on bad input)
              4. Handler invocation (catches all exceptions → structured)
              5. Audit log write + latency emit (regardless of outcome)

            All errors raise RuntimeError(structured_json) so the SDK
            sets isError=True and the agent gets the structured envelope
            in the message text.
            """
            t_start = time.monotonic()
            error_code: str | None = None
            success = False
            try:
                # 1. Rate limit
                try:
                    await self._rate_limiter.acquire()
                except ToolError as e:
                    error_code = e.code
                    raise RuntimeError(StructuredError.from_exception(e).to_text()) from e

                # 2. Tool lookup
                t = get_tool(name)
                if t is None:
                    err = StructuredError(
                        code="not_found",
                        message=f"unknown tool: {name!r}",
                        retriable=False,
                    )
                    error_code = "not_found"
                    raise RuntimeError(err.to_text())

                # 3. Input validation
                try:
                    input_obj = t.input_model.model_validate(arguments or {})
                except ValidationError as e:
                    err = StructuredError(
                        code="validation_failed",
                        message=f"invalid input for {name!r}: {e.errors()[:5]}",
                        retriable=False,
                    )
                    error_code = "validation_failed"
                    raise RuntimeError(err.to_text()) from e

                # 4. Handler invocation
                try:
                    result = await t.handler(ctx=self._ctx, input=input_obj)
                except Exception as e:
                    log.warning("mcp.tool.handler_failed",
                                tool=name,
                                tenant_id=str(self._ctx.tenant_id),
                                error_class=type(e).__name__,
                                error=str(e)[:300])
                    err = StructuredError.from_exception(e)
                    error_code = err.code
                    raise RuntimeError(err.to_text()) from e

                success = True
                text = json.dumps(result, default=_json_default)
                return [mcp_types.TextContent(type="text", text=text)]
            finally:
                # 5. Audit + latency. Best-effort; never poisons the call.
                ms = (time.monotonic() - t_start) * 1000.0
                emit_latency(
                    tool=name, tenant_id=self._ctx.tenant_id,
                    ms=ms, success=success, error_code=error_code,
                )
                try:
                    await self._audit_writer.record(
                        tenant_id=self._ctx.tenant_id,
                        tool_name=name,
                        latency_ms=ms,
                        success=success,
                        error_code=error_code,
                    )
                except Exception:  # noqa: S110  # audit-write failure must never break a tool call; the writer logs internally
                    pass


def _json_default(o: Any) -> Any:
    """Fallback for non-JSON-serializable types (UUID, datetime, etc).
    Pydantic model_dump(mode='json') already covers most cases; this is
    a safety net for ad-hoc dicts in tool returns."""
    from datetime import date, datetime
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")
