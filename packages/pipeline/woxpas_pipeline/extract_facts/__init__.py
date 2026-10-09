"""Extract-facts stage — Phase 2.1.

Subscribes to doc.indexed. Iterates over the document's chunks (non-overlapping,
pysbd-aware sentence boundaries from chunk stage), runs profile-aware
extraction LLM call per chunk, persists entities/ideas/tasks/claims with
chunk_id linkage. Emits doc.facts_extracted.

Module layout:
  prompt.py — profile-aware prompt builder + JSON schema + parser
  stage.py  — ExtractFactsStage class
"""

from woxpas_pipeline.extract_facts.stage import ExtractFactsStage

__all__ = ["ExtractFactsStage"]
