-- 033 — Team memory, Phase A1: member attribution
--
-- Records WHICH member a write is attributed to, alongside the existing
-- coarse captured_by role ('user'|'agent'|'system'). Added to the tables a
-- human member directly drives:
--   • documents            — who captured this memory      (captured_by_member_id)
--   • resurface_observations — whose agent session acted    (actor_member_id)
--       (record_decision writes here too, so decisions are covered)
--   • resurface_artifacts    — who a prepared action is for  (actor_member_id)
-- ideas / tasks_deadlines are pipeline-derived from a document, so they inherit
-- attribution via their source doc; direct columns can be added later if needed.
--
-- WIRING (design-forward, Pillar: future-proofing): the column DEFAULT reads
-- the per-request session GUC `app.member_id`, set by auth right after it sets
-- app.tenant_id (Phase A2). So every existing write path records attribution
-- automatically once A2 lands — no call-site changes — and stays NULL until
-- then (single-user / dev installs are unaffected). Mirrors how RLS already
-- reads app.tenant_id.
--
-- The column is added WITHOUT a default first (fast, no table rewrite — existing
-- rows become NULL), then the session-reading default is attached for new rows.

BEGIN;

-- helper: a member reference that self-heals when a member row is deleted
-- documents ─────────────────────────────────────────────────────────────────
ALTER TABLE documents
  ADD COLUMN captured_by_member_id uuid REFERENCES tenant_members(member_id) ON DELETE SET NULL;
ALTER TABLE documents
  ALTER COLUMN captured_by_member_id SET DEFAULT nullif(current_setting('app.member_id', true), '')::uuid;
CREATE INDEX documents_captured_by_member_idx
  ON documents(tenant_id, captured_by_member_id) WHERE captured_by_member_id IS NOT NULL;

-- resurface_observations (also covers decisions) ─────────────────────────────
ALTER TABLE resurface_observations
  ADD COLUMN actor_member_id uuid REFERENCES tenant_members(member_id) ON DELETE SET NULL;
ALTER TABLE resurface_observations
  ALTER COLUMN actor_member_id SET DEFAULT nullif(current_setting('app.member_id', true), '')::uuid;
CREATE INDEX resurface_observations_actor_member_idx
  ON resurface_observations(tenant_id, actor_member_id, observed_at DESC) WHERE actor_member_id IS NOT NULL;

-- resurface_artifacts (prepared actions) ─────────────────────────────────────
ALTER TABLE resurface_artifacts
  ADD COLUMN actor_member_id uuid REFERENCES tenant_members(member_id) ON DELETE SET NULL;
ALTER TABLE resurface_artifacts
  ALTER COLUMN actor_member_id SET DEFAULT nullif(current_setting('app.member_id', true), '')::uuid;
CREATE INDEX resurface_artifacts_actor_member_idx
  ON resurface_artifacts(tenant_id, actor_member_id) WHERE actor_member_id IS NOT NULL;

-- Expose member attribution through the stable document view (append at end,
-- preserving existing column positions — the resurfacing layer's only legal
-- read path, per the backend-stability commitment). security_invoker re-set.
CREATE OR REPLACE VIEW v_documents AS
 SELECT tenant_id,
    doc_id,
    title,
    filename,
    summary,
    topics_inferred AS topics,
    processing_profile,
    sensitivity_class,
    status,
    created_at,
    updated_at,
    last_accessed_at,
    access_count,
    canonical_uri,
    thread_id,
    COALESCE(( SELECT array_agg(s.slug ORDER BY s.slug) AS array_agg
           FROM (doc_scopes ds
             JOIN scopes s ON (((s.tenant_id = ds.tenant_id) AND (s.scope_id = ds.scope_id))))
          WHERE ((ds.tenant_id = d.tenant_id) AND (ds.doc_id = d.doc_id))), ARRAY[]::text[]) AS scope_slugs,
    captured_by,
    source_channel,
    captured_by_member_id
   FROM documents d
  WHERE ((deleted_at IS NULL) AND (archived_at IS NULL));

ALTER VIEW v_documents SET (security_invoker = true);

INSERT INTO schema_migrations (version) VALUES ('033_member_attribution');

COMMIT;
