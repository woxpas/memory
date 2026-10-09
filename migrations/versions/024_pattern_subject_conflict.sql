-- 024 — Extend resurface_subject_kind enum to include 'conflict'.
--
-- The 5a.2 PatternScorer initially worked around the missing value by
-- storing conflict_ids under subject_kind='idea' (per a TODO comment in
-- scorer.py). That broke label resolution since the engine looks up
-- ideas in v_ideas, not v_conflicts_open. Adding 'conflict' as a first-
-- class subject_kind is one ALTER TYPE — cheap fix.
--
-- ALTER TYPE ADD VALUE cannot run inside a transaction block before
-- Postgres 12+; PG17 supports it but the value isn't visible to other
-- statements within the same transaction. Run as a standalone
-- statement.

ALTER TYPE resurface_subject_kind ADD VALUE IF NOT EXISTS 'conflict';

INSERT INTO schema_migrations (version) VALUES ('024_pattern_subject_conflict');
