-- 025 — Indexes that make surface_score ordering O(log N) instead of O(N).
--
-- The v0.1 schema added decay_score / momentum_score / surface_score /
-- centrality_score columns to entities, ideas, documents — but never
-- wired up the maintenance job that POPULATES them. Phase 5a.2.1 adds
-- the maintenance job (ScoreMaintainer); these indexes ensure the hot
-- path ("top N entities/ideas by surface_score") is sub-millisecond at
-- any corpus size.
--
-- Partial indexes are used to keep them small — we only care about
-- "alive" rows (canonical entities, non-abandoned ideas, non-deleted
-- documents). DESC ordering matches the ORDER BY surface_score DESC
-- usage; NULLS LAST so freshly-inserted rows (score=NULL until
-- maintainer runs) sort to the end without disrupting the index scan.

BEGIN;

CREATE INDEX IF NOT EXISTS entities_surface_score_idx
  ON entities (tenant_id, surface_score DESC NULLS LAST)
  WHERE merged_into_entity_id IS NULL;

CREATE INDEX IF NOT EXISTS ideas_surface_score_idx
  ON ideas (tenant_id, surface_score DESC NULLS LAST)
  WHERE status NOT IN ('abandoned', 'realized');

CREATE INDEX IF NOT EXISTS documents_surface_score_idx
  ON documents (tenant_id, surface_score DESC NULLS LAST)
  WHERE deleted_at IS NULL;

-- Indexes used by the maintainer itself (find rows that need refresh —
-- ordered by last_mentioned_at / last_revisited_at / last_accessed_at).
-- Most of these already exist from earlier migrations; create-if-missing
-- to be safe.

CREATE INDEX IF NOT EXISTS entities_last_mentioned_idx
  ON entities (tenant_id, last_mentioned_at DESC NULLS LAST)
  WHERE merged_into_entity_id IS NULL;

CREATE INDEX IF NOT EXISTS ideas_last_revisited_idx
  ON ideas (tenant_id, last_revisited_at DESC NULLS LAST)
  WHERE status NOT IN ('abandoned', 'realized');

INSERT INTO schema_migrations (version) VALUES ('025_surface_score_indexes');

COMMIT;
