"""ToolContext — handles passed to every tool handler.

The MCP server constructs one ToolContext per tenant at startup, then
hands it to each tool invocation via the registry. Mirrors AppState's
construction (same engines, same warm embedder/reranker/LLM cache),
but without the FastAPI machinery.

Tenant binding is intrinsic — every tool reads ctx.tenant_id rather
than accepting a tenant_id argument. This is THE security boundary for
the MCP layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from woxpas_api.engines.conflict import ConflictEngine
    from woxpas_api.engines.document_admin import DocumentAdminEngine
    from woxpas_api.engines.document_read import DocumentReadEngine
    from woxpas_api.engines.scope_admin import ScopeAdminEngine
    from woxpas_pipeline import IngressStage, PipelineContext
    from woxpas_resurfacing import ResurfaceEngine
    from woxpas_retrieval import RetrievalEngine
    from woxpas_synthesis import SynthesisEngine


@dataclass(slots=True)
class ToolContext:
    """Handles for a single MCP server's tools. One per server process."""
    tenant_id:      UUID
    pipeline_ctx:   PipelineContext
    retrieval:      RetrievalEngine
    synthesis:      SynthesisEngine
    resurface:      ResurfaceEngine
    scope_admin:    ScopeAdminEngine
    document_admin: DocumentAdminEngine
    document_read:  DocumentReadEngine
    conflict:       ConflictEngine
    ingress:        IngressStage

    async def close(self) -> None:
        """Tear down underlying resources (pool, qdrant, etc)."""
        await self.pipeline_ctx.close()
