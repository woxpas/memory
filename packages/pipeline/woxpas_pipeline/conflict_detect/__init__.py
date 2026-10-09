"""Conflict-detection stage — Phase 2.3.

Subscribes to doc.linked. Three detection subroutines, all SQL-only:

  1. Knowledge mismatch     — relations: same (subject, predicate_canonical),
                              different object across docs.
  2. Preference mismatch    — same as above but predicate.kind = preference.
                              Severity=low (opinions evolve; not as harsh).
  3. Profile fact mismatch  — entity_profile_facts: same (entity, fact_kind),
                              different fact_value.

Idempotent on re-run via dedup_key. Detection writes 'open' rows; resolution
is a separate explicit action (user / agent / v0.2 sweep job).
"""

from woxpas_pipeline.conflict_detect.stage import ConflictDetectionStage

__all__ = ["ConflictDetectionStage"]
