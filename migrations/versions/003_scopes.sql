-- 003 — Scopes (multi-membership virtual folders)
--
-- A scope is a filter dimension, not a partition. Documents can belong to many scopes;
-- entities/conflicts/relations remain global so cross-scope memory connections survive.
-- Embedding refs + member centroids enable semantic dedup ("don't create a duplicate scope").

CREATE TABLE scopes (
  scope_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  name                  text NOT NULL,
  slug                  text NOT NULL,
  description           text,
  parent_scope_id       uuid REFERENCES scopes(scope_id) ON DELETE SET NULL,

  -- presentation
  color                 text,
  icon                  text,
  pinned                boolean NOT NULL DEFAULT false,
  sort_order            int NOT NULL DEFAULT 0,

  -- semantic dedup substrate (vectors live in Qdrant scopes_collection)
  embedding_ref         uuid,                  -- description-embedding point id
  member_centroid_ref   uuid,                  -- centroid-of-members point id
  member_count          int NOT NULL DEFAULT 0,
  last_centroid_update  timestamptz,

  -- surfacing
  decay_score           real NOT NULL DEFAULT 1.0,
  momentum_score        real NOT NULL DEFAULT 0.0,
  surface_score         real NOT NULL DEFAULT 0.0,
  last_seen_at          timestamptz,

  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb,
  archived_at           timestamptz,

  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT scopes_slug_unique UNIQUE (tenant_id, slug),
  CONSTRAINT scopes_no_self_parent CHECK (parent_scope_id IS DISTINCT FROM scope_id)
);
CREATE INDEX scopes_tenant_idx       ON scopes(tenant_id);
CREATE INDEX scopes_parent_idx       ON scopes(parent_scope_id) WHERE parent_scope_id IS NOT NULL;
CREATE INDEX scopes_name_trgm        ON scopes USING gin (name gin_trgm_ops);
CREATE INDEX scopes_description_trgm ON scopes USING gin (description gin_trgm_ops)
                                       WHERE description IS NOT NULL;
CREATE INDEX scopes_active_idx       ON scopes(tenant_id, sort_order)
                                       WHERE archived_at IS NULL;

-- Doc <-> Scope (many-to-many). FK to documents added in 011 (after documents exists).
CREATE TABLE doc_scopes (
  doc_id        uuid NOT NULL,
  scope_id      uuid NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,
  tenant_id     uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  assigned_at   timestamptz NOT NULL DEFAULT now(),
  assigned_by   text NOT NULL
                  CHECK (assigned_by IN ('user','classifier','agent','adapter','rule')),
  auto_assigned boolean NOT NULL DEFAULT false,
  confidence    real,
  PRIMARY KEY (doc_id, scope_id)
);
CREATE INDEX doc_scopes_scope_idx  ON doc_scopes(scope_id);
CREATE INDEX doc_scopes_tenant_idx ON doc_scopes(tenant_id);
CREATE INDEX doc_scopes_doc_idx    ON doc_scopes(doc_id);

-- Auto-assignment rules (e.g., "if source_channel = slack#workout-buddies, assign to scope:workout").
CREATE TABLE scope_rules (
  rule_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  scope_id     uuid NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,
  rule_type    text NOT NULL
                CHECK (rule_type IN (
                  'pattern_filename','pattern_content','tag_match',
                  'source_channel','embedding_match','entity_present'
                )),
  rule_def     jsonb NOT NULL,
  priority     int NOT NULL DEFAULT 100,
  active       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scope_rules_active_idx ON scope_rules(tenant_id, priority DESC) WHERE active;

INSERT INTO schema_migrations (version) VALUES ('003_scopes');
