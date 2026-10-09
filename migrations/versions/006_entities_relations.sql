-- 006 — Entities, aliases, mentions, relations
--
-- Schema slots populated by the EXTRACTION pipeline (separate build).
-- Ingestion does not write here; it just guarantees the slots exist so foreign keys
-- and contracts are stable from day one.
--
-- Entities are GLOBAL across scopes (a person is the same person whether mentioned in
-- workout or work). Scope filtering happens at query time via mention.doc_id → doc.scopes.

CREATE TABLE entities (
  entity_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  canonical_name   text NOT NULL,
  type             text NOT NULL
                    CHECK (type IN (
                      'person','organization','location','product','project',
                      'concept','topic','event','tool','document_ref','other'
                    )),

  profile          jsonb NOT NULL DEFAULT '{}'::jsonb,    -- accreted by extraction over time

  -- vector for semantic entity-match retrieval (lives in Qdrant entities_collection)
  embedding_ref    uuid,
  embedding_model  text,

  -- surfacing
  decay_score      real NOT NULL DEFAULT 1.0,
  momentum_score   real NOT NULL DEFAULT 0.0,
  surface_score    real NOT NULL DEFAULT 0.0,
  centrality_score real NOT NULL DEFAULT 0.0,
  mention_count    int  NOT NULL DEFAULT 0,
  last_mentioned_at timestamptz,
  first_mentioned_at timestamptz,

  -- bitemporal: when does the system know this; when is the entity "valid"
  system_time      tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time       tstzrange,

  merged_into_entity_id uuid REFERENCES entities(entity_id) ON DELETE SET NULL,

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT entities_canonical_unique UNIQUE (tenant_id, type, canonical_name)
);
CREATE INDEX entities_tenant_type_idx     ON entities(tenant_id, type);
CREATE INDEX entities_canonical_trgm      ON entities USING gin (canonical_name gin_trgm_ops);
CREATE INDEX entities_decay_idx           ON entities(tenant_id, decay_score DESC);
CREATE INDEX entities_momentum_idx        ON entities(tenant_id, momentum_score DESC);
CREATE INDEX entities_merged_idx          ON entities(merged_into_entity_id)
                                            WHERE merged_into_entity_id IS NOT NULL;

-- Surface forms / aliases (Jaccard / fuzzy matching at retrieval time uses these).
CREATE TABLE entity_aliases (
  alias_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id       uuid NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
  tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  surface_form    text NOT NULL,
  normalized      text NOT NULL,
  source          text NOT NULL DEFAULT 'extraction'
                   CHECK (source IN ('extraction','user','agent','import')),
  confidence      real,
  occurrences     int  NOT NULL DEFAULT 0,

  created_at      timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT entity_aliases_unique UNIQUE (tenant_id, entity_id, normalized)
);
CREATE INDEX entity_aliases_surface_trgm ON entity_aliases USING gin (surface_form gin_trgm_ops);
CREATE INDEX entity_aliases_norm_idx     ON entity_aliases(tenant_id, normalized);

-- Mentions: every place an entity is referenced. Provenance to span granularity.
CREATE TABLE entity_mentions (
  mention_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id      uuid NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
  tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  doc_id         uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  block_id       uuid REFERENCES canonical_blocks(block_id) ON DELETE SET NULL,
  span_id        uuid REFERENCES spans(span_id) ON DELETE SET NULL,
  chunk_id       uuid REFERENCES chunks(chunk_id) ON DELETE SET NULL,

  surface_form   text NOT NULL,
  confidence     real,

  -- bitemporal
  system_time    tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time     tstzrange,

  extractor      text,
  extractor_version text,

  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entity_mentions_entity_idx     ON entity_mentions(entity_id, created_at DESC);
CREATE INDEX entity_mentions_doc_idx        ON entity_mentions(doc_id);
CREATE INDEX entity_mentions_chunk_idx      ON entity_mentions(chunk_id) WHERE chunk_id IS NOT NULL;
CREATE INDEX entity_mentions_span_idx       ON entity_mentions(span_id) WHERE span_id IS NOT NULL;
CREATE INDEX entity_mentions_valid_time_gst ON entity_mentions USING gist (valid_time)
                                              WHERE valid_time IS NOT NULL;

-- Subject-predicate-object claims. Bitemporal so "was true / is now / will be" is modelable.
-- Conflict detection (Type 2) reads this table.
CREATE TABLE relations (
  relation_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  subject_entity_id  uuid NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
  predicate          text NOT NULL,                              -- e.g., "costs", "lives_in", "works_at"
  predicate_kind     text NOT NULL DEFAULT 'open'
                       CHECK (predicate_kind IN ('open','typed','functional')),
  -- functional = at most one value per (subject, predicate, valid_time);
  -- typed     = predicate from a controlled vocabulary;
  -- open      = free-form.

  object_entity_id   uuid REFERENCES entities(entity_id) ON DELETE SET NULL,
  object_value       jsonb,                                      -- {kind: 'literal', value, unit?}

  -- provenance
  doc_id             uuid REFERENCES documents(doc_id) ON DELETE SET NULL,
  block_id           uuid REFERENCES canonical_blocks(block_id) ON DELETE SET NULL,
  span_id            uuid REFERENCES spans(span_id) ON DELETE SET NULL,
  chunk_id           uuid REFERENCES chunks(chunk_id) ON DELETE SET NULL,

  -- bitemporal
  system_time        tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time         tstzrange,

  confidence         real,
  extractor          text,
  extractor_version  text,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX relations_subject_predicate_idx ON relations(tenant_id, subject_entity_id, predicate);
CREATE INDEX relations_object_entity_idx     ON relations(object_entity_id) WHERE object_entity_id IS NOT NULL;
CREATE INDEX relations_doc_idx               ON relations(doc_id) WHERE doc_id IS NOT NULL;
CREATE INDEX relations_valid_time_gist       ON relations USING gist (valid_time)
                                                WHERE valid_time IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('006_entities_relations');
