"""Tool implementations are imported here so the registry decorators
fire at module-import time. The server's startup imports this module
to populate the registry.

Memory tools (13):
  memory_recall              — query → chunks/cards/lists/graph (intent-routed)
  memory_ask                 — citation-verified synthesis (cloud-gated)
  memory_get_chunks          — hydrate chunk_ids to full text (batch)
  memory_ingest              — capture text content into the pipeline
  memory_get_document_full   — full doc + all chunks + optional canonical/raw
  memory_create_scope        — new scope with optional embedding
  memory_assign_to_scope     — attach doc to scope by slug
  memory_unassign_from_scope — remove doc-scope membership
  memory_archive_document    — exclude doc from retrieval
  memory_unarchive_document  — restore archived doc
  memory_list_conflicts      — list open/resolved conflicts
  memory_get_conflict        — full conflict detail + suggested shapes
  memory_resolve_conflict    — apply pick/coexistence/progression/dismissed
"""

from woxpas_mcp.tools import (
    memory_archive,  # noqa: F401
    memory_ask,  # noqa: F401
    memory_conflicts,  # noqa: F401
    memory_get_chunks,  # noqa: F401
    memory_get_document_full,  # noqa: F401
    memory_ingest,  # noqa: F401
    memory_recall,  # noqa: F401
    memory_scopes,  # noqa: F401
    resurface_actions,  # noqa: F401
    resurface_identity,  # noqa: F401
    resurface_thinking,  # noqa: F401
    resurface_writes,  # noqa: F401
)
