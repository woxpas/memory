-- 018 — Relations archive support.
--
-- Adds archived_at + superseded_by to relations (symmetric with what
-- entity_profile_facts already has). Phase 2.3 conflict-detection's
-- 'pick' resolution sets archived_at on losers; default retrieval
-- queries WHERE archived_at IS NULL. Provenance preserved for history mode.
--
-- superseded_by points to the winning relation_id so retrieval can follow
-- the chain ("what did this used to say"). NULL for live (unarchived) rows
-- and for losers without a clear winner (coexistence/dismissed cases).

ALTER TABLE relations
  ADD COLUMN archived_at  timestamptz,
  ADD COLUMN superseded_by uuid REFERENCES relations(relation_id) ON DELETE SET NULL;

-- Default-retrieval index: live relations only.
CREATE INDEX relations_live_idx
  ON relations(tenant_id, subject_entity_id, predicate_canonical)
  WHERE archived_at IS NULL;

-- History-retrieval index: chase chains of supersession.
CREATE INDEX relations_superseded_by_idx
  ON relations(superseded_by)
  WHERE superseded_by IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('018_relations_archive');
