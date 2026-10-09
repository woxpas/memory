-- 019 — Conflicts table additions for Phase 2.3 detection.
--
-- The base conflicts table from migration 007 is rich enough — jsonb
-- mention_refs, array subject_entity_ids, full resolved_state enum
-- (open|auto_resolved|human_resolved|living|dismissed). We add only what
-- Phase 2.3 detection needs:
--
--   1. dedup_key — opaque string the detection stage computes from the
--      (sorted) IDs of the conflicting items. Unique-indexed so re-running
--      detection on the same pair is a safe no-op (ON CONFLICT DO NOTHING).
--
-- The existing `resolved_value jsonb` column carries the Phase 2.3
-- resolution shape:
--
--   resolved_value = {
--     "kind":     "pick" | "coexistence" | "progression" | "dismissed",
--     "winner":   "<id|null>",                    # for pick + progression-latest
--     "loser":    "<id|null>",                    # for pick
--     "discriminators": {                          # for coexistence
--       "claim_a": "home address", "claim_b": "work address"
--     },
--     "valid_times": {                             # for progression
--       "<id>": "[2020-01-01,2022-06-15)", ...
--     }
--   }
--
-- The existing `notes` text column carries the human-readable resolution
-- reason ("Sid moved Berlin→Paris in March 2026 for new job"). Both fields
-- are populated by the resolver (user, agent, or v0.2 background sweep).
--
-- The existing 'living' resolved_state is the right fit for the
-- "coexistence" kind — both values stand, neither archived.

ALTER TABLE conflicts
  ADD COLUMN dedup_key text;

CREATE UNIQUE INDEX conflicts_dedup_uidx
  ON conflicts(tenant_id, dedup_key)
  WHERE dedup_key IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('019_conflicts');
