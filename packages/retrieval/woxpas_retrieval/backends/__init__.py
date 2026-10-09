"""Retrieval backends — one module per backend strategy.

  db_direct — pure SQL queries (timeline, type listings, file lookup, entity card)
  vector    — hybrid dense + sparse + ColBERT over Qdrant (Phase 3.3)
  graph     — recursive CTE on relations (Phase 3.6)

The orchestrator (Phase 3.8) dispatches by intent and composes results.
"""

from woxpas_retrieval.backends.db_direct import DbDirectBackend
from woxpas_retrieval.backends.graph import (
    GraphBackend,
    consolidate_edges_by_pair,
)
from woxpas_retrieval.backends.vector import VectorBackend

__all__ = [
    "DbDirectBackend", "GraphBackend", "VectorBackend",
    "consolidate_edges_by_pair",
]
