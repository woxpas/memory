-- 034 — Team memory, Phase A2b: per-member preferences & identity
--
-- Each collaborator gets their own answer-length / citation / persona-tone /
-- tool-use profile, instead of one profile per tenant. A row with member_id
-- NULL is the TEAM DEFAULT — used by background jobs (the dreaming worker mines
-- tenant-wide tool-use patterns with no member context) and as the fallback
-- when a member has no profile of their own yet.
--
-- Identity needs NO schema change here: each member's personal self-entity
-- already lives on tenant_members.self_entity_id (migration 032), and
-- identity_card(entity_id, …) is already entity-parameterized — the MCP layer
-- just passes the acting member's self-entity (wired in resurface_build_identity).
--
-- Re-key: drop the (tenant_id) primary key and add a UNIQUE index over
-- (tenant_id, COALESCE(member_id, <sentinel>)). The COALESCE collapses NULL to
-- a fixed sentinel so (a) there is exactly one team-default row per tenant and
-- (b) upserts can use ONE conflict target for both member and team-default rows.
-- Existing rows keep member_id NULL, so they simply BECOME the team default —
-- no backfill, and single-user installs are unchanged.

BEGIN;

ALTER TABLE resurface_user_preferences
  DROP CONSTRAINT resurface_user_preferences_pkey;

ALTER TABLE resurface_user_preferences
  ADD COLUMN member_id uuid REFERENCES tenant_members(member_id) ON DELETE CASCADE;

CREATE UNIQUE INDEX uq_prefs_tenant_member
  ON resurface_user_preferences
     (tenant_id, COALESCE(member_id, '00000000-0000-0000-0000-000000000000'::uuid));

INSERT INTO schema_migrations (version) VALUES ('034_per_member_preferences');

COMMIT;
