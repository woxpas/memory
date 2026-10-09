"""Sensitivity classification — three-tier OR-combined system.

Tier 1 — Manual user flag at /ingest/file (highest priority, set at ingestion)
Tier 2 — Summarize-LLM judgment (is_sensitive in summary JSON output)
Tier 3 — Heuristics + summary topic semantics (post-summarize refine stage)
Tier 3a — Hard-heuristics subset of Tier 3, run early in detect stage so the
          summarize-stage cloud gate has something to block on.

Combination rule: any tier flag → sensitive. Downgrade only via explicit
manual override (woxpas-cli mark-sensitive --value standard).

Module layout:
  heuristics.py — pure detection functions (no DB, no I/O)
  refine.py     — Tier 3 stage logic (combines heuristics + summary topics)
"""

from woxpas_pipeline.sensitivity.heuristics import (
    HardHit,
    SoftHit,
    hard_tier_scan,
    soft_tier_scan,
)
from woxpas_pipeline.sensitivity.refine import SensitivityRefineStage

__all__ = [
    "HardHit",
    "SensitivityRefineStage",
    "SoftHit",
    "hard_tier_scan",
    "soft_tier_scan",
]
