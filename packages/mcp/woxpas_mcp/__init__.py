"""woxpas-mcp — agent-facing MCP tool surface for Woxpas memory.

This package wraps the engine layer (in packages/api/woxpas_api/engines/)
+ ResurfaceEngine + RetrievalEngine + SynthesisEngine as MCP tools any
MCP-capable agent (Claude Desktop, Cursor, custom) can consume.

Architecture (project_phase5a5_mcp_plan.md §3):
  - Stdio transport v0.1; HTTP/SSE adapter in v0.2
  - Tenant_id from env (WOXPAS_MCP_TENANT_ID); refuse to start without
  - In-process engine reuse (mirrors AppState.construct pattern)
  - 25 tools split across memory.* (13) and resurface.* (12)
"""

from woxpas_mcp.server import WoxpasMcpServer
from woxpas_mcp.settings import McpSettings

__all__ = ["McpSettings", "WoxpasMcpServer"]
