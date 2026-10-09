-- 021 — Retrieval access tracking on entities / ideas / tasks_deadlines.
--
-- These tables already track ingest-time signals (mention_count, last_mentioned_at
-- on entities; mention_count, last_revisited_at on ideas) but those measure
-- WHEN-EXTRACTED, not WHEN-RETRIEVED. Phase 3.7 retrieval-enrichment bumps
-- access_count + last_accessed_at on every retrieved item. Those signals power
-- decay/momentum/surface-score calculations distinct from ingest centrality.
--
-- documents already has these two columns from v0.1 — pattern unchanged here.
-- entity_profile_facts deliberately excluded: profile facts are a static
-- view (identity card snapshot) — measuring retrieval access on individual
-- facts isn't useful at this layer.

ALTER TABLE entities
  ADD COLUMN access_count     integer     NOT NULL DEFAULT 0,
  ADD COLUMN last_accessed_at timestamptz;

ALTER TABLE ideas
  ADD COLUMN access_count     integer     NOT NULL DEFAULT 0,
  ADD COLUMN last_accessed_at timestamptz;

ALTER TABLE tasks_deadlines
  ADD COLUMN access_count     integer     NOT NULL DEFAULT 0,
  ADD COLUMN last_accessed_at timestamptz;

-- Indexes for "what's been accessed recently" / "what's stale" queries.
CREATE INDEX entities_last_accessed_idx
  ON entities(tenant_id, last_accessed_at DESC NULLS LAST);
CREATE INDEX ideas_last_accessed_idx
  ON ideas(tenant_id, last_accessed_at DESC NULLS LAST);
CREATE INDEX tasks_deadlines_last_accessed_idx
  ON tasks_deadlines(tenant_id, last_accessed_at DESC NULLS LAST);

INSERT INTO schema_migrations (version) VALUES ('021_retrieval_access_tracking');
