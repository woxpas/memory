-- 020 — Extend conflicts.type CHECK with explicit Phase 2.3 detection kinds.
--
-- The existing enum from migration 007 was:
--   supersession | value | knowledge | temporal | opinion | timeslot | reference
--
-- Phase 2.3 detection writes:
--   knowledge_mismatch     — same (subject, predicate_canonical), different object
--   preference_mismatch    — same as above but predicate has kind=preference
--   profile_fact_mismatch  — same (entity, fact_kind), different fact_value in
--                            entity_profile_facts
--
-- Adding the explicit values rather than reusing 'knowledge' / 'opinion' so
-- the type clearly tells callers WHICH stage detected the conflict and on
-- WHICH table (relations vs entity_profile_facts), not just the semantic
-- shape. Existing values stay valid for backward compat.

ALTER TABLE conflicts DROP CONSTRAINT IF EXISTS conflicts_type_check;

ALTER TABLE conflicts
  ADD CONSTRAINT conflicts_type_check CHECK (type IN (
    -- v0.1 — kept for backward compat (no rows yet, but constraint preserved)
    'supersession', 'value', 'knowledge', 'temporal', 'opinion', 'timeslot', 'reference',
    -- Phase 2.3 — explicit detection kinds
    'knowledge_mismatch', 'preference_mismatch', 'profile_fact_mismatch',
    -- Phase 2.3 — temporal supersession via the existing supersession_check stage,
    -- when (v0.2) it gets unified into the conflicts table.
    'temporal_supersession'
  ));

INSERT INTO schema_migrations (version) VALUES ('020_conflict_type_extension');
