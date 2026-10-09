-- 012 — Extraction-pipeline foundation.
--
-- Locked design from planning:
--   • 10-profile consolidated processing_profile (down from 16)
--   • tenants.self_entity_id FK
--   • relations.claim_text + predicate_canonical (hybrid claim storage)
--   • predicates registry (global + per-tenant via NULL-or-tenant RLS)
--   • ideas/tasks_deadlines.owner_entity_id FK
--   • entity_profile_facts (typed-key facts) + FK join table for source mentions
--   • identity_card(entity_id, ...limits...) SQL function

-- ─── 1. tenants.self_entity_id ─────────────────────────────────────────────
ALTER TABLE tenants
  ADD COLUMN self_entity_id uuid REFERENCES entities(entity_id) ON DELETE RESTRICT;

CREATE INDEX tenants_self_entity_idx
  ON tenants(self_entity_id) WHERE self_entity_id IS NOT NULL;


-- ─── 2. documents.processing_profile — consolidate to 10 + new values ──────
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_processing_profile_check;

UPDATE documents SET processing_profile = 'personal'
  WHERE processing_profile IN ('thought','voice_note','mixed_note');
UPDATE documents SET processing_profile = 'creative_artefact'
  WHERE processing_profile = 'artefact_lyric';
UPDATE documents SET processing_profile = 'contract'
  WHERE processing_profile = 'artefact_contract';
UPDATE documents SET processing_profile = 'reference'
  WHERE processing_profile IN ('artefact_reference','book','paper','manual');
UPDATE documents SET processing_profile = 'correspondence'
  WHERE processing_profile IN ('email','transcript');
-- correspondence, calendar_event, code, custom unchanged

ALTER TABLE documents ADD CONSTRAINT documents_processing_profile_check CHECK (
  processing_profile IN (
    'personal',
    'identity_seed',
    'creative_artefact',
    'contract',
    'reference',
    'code',
    'correspondence',
    'calendar_event',
    'agent_log',
    'custom'
  )
);


-- ─── 3. relations.claim_text + relations.predicate_canonical ───────────────
ALTER TABLE relations ADD COLUMN claim_text          text;
ALTER TABLE relations ADD COLUMN predicate_canonical text;

CREATE INDEX relations_subject_canonical_idx
  ON relations(tenant_id, subject_entity_id, predicate_canonical)
  WHERE predicate_canonical IS NOT NULL;

CREATE INDEX relations_canonical_idx
  ON relations(tenant_id, predicate_canonical)
  WHERE predicate_canonical IS NOT NULL;


-- ─── 4. predicates registry (global + per-tenant) ──────────────────────────
CREATE TABLE predicates (
  predicate_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid REFERENCES tenants(tenant_id) ON DELETE CASCADE,    -- NULL = global
  canonical       text NOT NULL,
  description     text,
  aliases         text[] NOT NULL DEFAULT '{}',
  kind            text NOT NULL CHECK (kind IN (
                    'attribute',
                    'relation',
                    'temporal',
                    'quantitative',
                    'locational',
                    'communicative',
                    'reference',
                    'state',
                    'preference',     -- emotional/cognitive; conflicts treated as 'opinion'
                    'other'
                  )),
  argument_types  jsonb NOT NULL DEFAULT '{}'::jsonb,
  embedding_ref   uuid,                                  -- → Qdrant predicates_collection (later)
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT predicates_canonical_unique UNIQUE NULLS NOT DISTINCT (tenant_id, canonical)
);

CREATE INDEX predicates_kind_idx     ON predicates(kind);
CREATE INDEX predicates_aliases_gin  ON predicates USING gin (aliases);
CREATE INDEX predicates_global_idx   ON predicates(canonical) WHERE tenant_id IS NULL;

-- RLS: read globals OR own; write/update/delete own only.
-- NOT FORCE'd — owner role seeds globals via 013 + future migrations.
ALTER TABLE predicates ENABLE ROW LEVEL SECURITY;

CREATE POLICY predicates_read ON predicates FOR SELECT
  USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY predicates_insert ON predicates FOR INSERT
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY predicates_update ON predicates FOR UPDATE
  USING      (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY predicates_delete ON predicates FOR DELETE
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON predicates TO woxpas_app;


-- ─── 5. ideas + tasks_deadlines: owner_entity_id ───────────────────────────
ALTER TABLE ideas
  ADD COLUMN owner_entity_id uuid REFERENCES entities(entity_id) ON DELETE SET NULL;

CREATE INDEX ideas_owner_idx
  ON ideas(tenant_id, owner_entity_id) WHERE owner_entity_id IS NOT NULL;

ALTER TABLE tasks_deadlines
  ADD COLUMN owner_entity_id uuid REFERENCES entities(entity_id) ON DELETE SET NULL;

CREATE INDEX tasks_owner_idx
  ON tasks_deadlines(tenant_id, owner_entity_id) WHERE owner_entity_id IS NOT NULL;


-- ─── 6. entity_profile_facts (typed-key facts) ────────────────────────────
CREATE TABLE entity_profile_facts (
  fact_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id         uuid NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
  tenant_id         uuid NOT NULL REFERENCES tenants(tenant_id)  ON DELETE CASCADE,

  fact_category     text NOT NULL CHECK (fact_category IN (
                      'bio','profession','comm_style','projects','preferences',
                      'expertise','relationships','goals','health','other'
                    )),
  fact_kind         text NOT NULL,
  fact_value        jsonb NOT NULL,

  confidence        real NOT NULL DEFAULT 1.0,
  is_user_asserted  boolean NOT NULL DEFAULT false,
  -- source_mentions live in entity_profile_fact_sources (FK + CASCADE join table)

  system_time       tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time        tstzrange,

  superseded_by     uuid REFERENCES entity_profile_facts(fact_id) ON DELETE SET NULL,

  extractor         text,
  extractor_version text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX epf_entity_category_idx
  ON entity_profile_facts(tenant_id, entity_id, fact_category)
  WHERE superseded_by IS NULL;

CREATE INDEX epf_entity_kind_idx
  ON entity_profile_facts(tenant_id, entity_id, fact_kind)
  WHERE superseded_by IS NULL;

CREATE INDEX epf_user_asserted_idx
  ON entity_profile_facts(tenant_id, entity_id)
  WHERE is_user_asserted AND superseded_by IS NULL;

SELECT woxpas_apply_tenant_rls('entity_profile_facts'::regclass);


-- ─── 6b. entity_profile_fact_sources (FK + CASCADE join table) ────────────
-- Provenance with strict referential integrity. Mention deletion cascades
-- away the join row but the fact survives, just losing that source.

CREATE TABLE entity_profile_fact_sources (
  fact_id     uuid NOT NULL REFERENCES entity_profile_facts(fact_id) ON DELETE CASCADE,
  mention_id  uuid NOT NULL REFERENCES entity_mentions(mention_id)    ON DELETE CASCADE,
  tenant_id   uuid NOT NULL REFERENCES tenants(tenant_id)             ON DELETE CASCADE,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (fact_id, mention_id)
);

CREATE INDEX epfs_mention_idx ON entity_profile_fact_sources(mention_id);
CREATE INDEX epfs_tenant_idx  ON entity_profile_fact_sources(tenant_id);

SELECT woxpas_apply_tenant_rls('entity_profile_fact_sources'::regclass);


-- ─── 7. identity_card() — agent's user-card; parameterized limits ─────────
-- NULL on any limit param = unbounded for that section.
CREATE OR REPLACE FUNCTION identity_card(
    p_entity_id    uuid,
    p_max_tasks    int DEFAULT 50,
    p_max_ideas    int DEFAULT 50,
    p_max_mentions int DEFAULT 20
) RETURNS jsonb
LANGUAGE sql STABLE
AS $$
SELECT jsonb_build_object(
    'entity_id', p_entity_id,

    -- Stored facts grouped by category, only non-superseded; user-asserted first.
    -- Sources aggregated from the join table per fact.
    'facts', COALESCE((
        SELECT jsonb_object_agg(fact_category, facts_arr)
        FROM (
            SELECT fact_category, jsonb_agg(jsonb_build_object(
                'fact_id',          f.fact_id,
                'kind',             f.fact_kind,
                'value',            f.fact_value,
                'confidence',       f.confidence,
                'is_user_asserted', f.is_user_asserted,
                'sources',          COALESCE(
                    (SELECT array_agg(s.mention_id)
                       FROM entity_profile_fact_sources s
                      WHERE s.fact_id = f.fact_id),
                    '{}'::uuid[]
                ),
                'valid_time',       f.valid_time
            ) ORDER BY f.is_user_asserted DESC, f.confidence DESC, f.created_at DESC) AS facts_arr
            FROM entity_profile_facts f
            WHERE f.entity_id = p_entity_id AND f.superseded_by IS NULL
            GROUP BY f.fact_category
        ) g
    ), '{}'::jsonb),

    -- Live: open tasks owned by this entity. NULL = unbounded.
    'open_tasks', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'task_id', t.task_id, 'title', t.title, 'status', t.status,
            'due_at', t.due_at, 'priority', t.priority,
            'recurrence_rule', t.recurrence_rule
        ) ORDER BY t.due_at NULLS LAST, t.created_at DESC)
        FROM (
            SELECT *
              FROM tasks_deadlines
             WHERE owner_entity_id = p_entity_id
               AND status NOT IN ('done','cancelled')
             ORDER BY due_at NULLS LAST, created_at DESC
             LIMIT COALESCE(p_max_tasks, 9999999)
        ) t
    ), '[]'::jsonb),

    -- Live: active ideas owned by this entity.
    'active_ideas', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'idea_id', i.idea_id, 'title', i.title, 'summary', i.summary,
            'status', i.status, 'momentum_score', i.momentum_score
        ) ORDER BY i.momentum_score DESC, i.originated_at DESC)
        FROM (
            SELECT *
              FROM ideas
             WHERE owner_entity_id = p_entity_id
               AND status IN ('raw','developing','active')
             ORDER BY momentum_score DESC, originated_at DESC
             LIMIT COALESCE(p_max_ideas, 9999999)
        ) i
    ), '[]'::jsonb),

    -- Live: recent doc mentions of this entity.
    'recent_mentions', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'mention_id', m.mention_id, 'doc_id', m.doc_id,
            'created_at', m.created_at
        ) ORDER BY m.created_at DESC)
        FROM (
            SELECT mention_id, doc_id, created_at
              FROM entity_mentions
             WHERE entity_id = p_entity_id
             ORDER BY created_at DESC
             LIMIT COALESCE(p_max_mentions, 9999999)
        ) m
    ), '[]'::jsonb)
);
$$;

GRANT EXECUTE ON FUNCTION identity_card(uuid, int, int, int) TO woxpas_app;


INSERT INTO schema_migrations (version) VALUES ('012_extraction_foundation');
