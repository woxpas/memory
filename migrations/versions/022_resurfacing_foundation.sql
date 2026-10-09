-- 022 — Resurfacing foundation: views layer + tables migration.
--
-- This migration introduces Phase 5a's stable contract:
--
--  (A) PUBLIC VIEW LAYER — the only interface resurfacing/agent code reads.
--      Insulates resurfacing from memory backend changes (Pillar 4).
--      If v0.2+ swaps pgvector → FAISS, renames columns, or changes encryption
--      envelopes, the views adapt; resurfacing/agent code stays unchanged.
--      Resurfacing is FORBIDDEN from SELECTing from base tables directly.
--
--  (B) RESURFACING-OWNED TABLES — five tables resurfacing controls:
--      resurface_observations, resurface_artifacts, resurface_dreaming_runs,
--      resurface_patterns_cache, resurface_user_preferences.
--      All RLS-isolated. Agent layer registers via observations + preferences.
--
--  (C) TENANT FLAGS — allow_cloud_for_background_jobs (independent from
--      synthesis-time cloud flag) + budget_caps (jsonb for per-agent token
--      caps used by the dreaming budget controls).
--
-- All five new tables get RLS via woxpas_apply_tenant_rls (defined in 009).
-- Encryption-at-rest applies via the application layer's existing AES-GCM
-- envelope to fields containing user-derived content (tool_input, content
-- jsonb where indicated).

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────
-- Section A — Public view layer (the BACKEND-STABILITY contract)
-- ─────────────────────────────────────────────────────────────────────────
--
-- These views expose a constant semantic shape over memory's internal
-- tables. When memory evolves in v0.2+, update the view definitions here;
-- consumers stay unchanged.
--
-- Views inherit RLS from base tables (tenant_id filter propagates through
-- the view). No additional RLS policies needed on regular views.

-- v_chunks: text content, doc context, sensitivity. Used by resurfacing's
-- recall enrichment + dreaming connection synthesis.
CREATE OR REPLACE VIEW v_chunks AS
SELECT
    c.tenant_id,
    c.chunk_id,
    c.doc_id,
    c.ordinal,
    c.text,
    c.token_count,
    d.title              AS doc_title,
    d.filename           AS doc_filename,
    d.topics_inferred    AS doc_topics,
    d.processing_profile AS doc_processing_profile,
    d.sensitivity_class  AS sensitivity_class,
    d.created_at         AS doc_created_at,
    d.last_accessed_at   AS doc_last_accessed_at,
    d.access_count       AS doc_access_count
  FROM chunks c
  JOIN documents d ON d.tenant_id = c.tenant_id
                  AND d.doc_id    = c.doc_id
                  AND d.deleted_at IS NULL;

-- v_documents: document-level signals + scope memberships + counters.
CREATE OR REPLACE VIEW v_documents AS
SELECT
    d.tenant_id,
    d.doc_id,
    d.title,
    d.filename,
    d.summary,
    d.topics_inferred       AS topics,
    d.processing_profile,
    d.sensitivity_class,
    d.status,
    d.created_at,
    d.updated_at,
    d.last_accessed_at,
    d.access_count,
    d.canonical_uri,
    d.thread_id,
    -- Scope memberships, materialized as slug array
    COALESCE(
      (SELECT array_agg(s.slug ORDER BY s.slug)
         FROM doc_scopes ds
         JOIN scopes s ON s.tenant_id = ds.tenant_id
                      AND s.scope_id  = ds.scope_id
        WHERE ds.tenant_id = d.tenant_id
          AND ds.doc_id    = d.doc_id),
      ARRAY[]::text[]
    ) AS scope_slugs
  FROM documents d
 WHERE d.deleted_at IS NULL;

-- v_entities: canonical (post-merge) entity rows with mention + access
-- signals. Self-entity identified via tenants.self_entity_id (no is_self
-- column on entities; check via JOIN against tenants if needed).
CREATE OR REPLACE VIEW v_entities AS
SELECT
    e.tenant_id,
    e.entity_id,
    e.canonical_name        AS name,
    e.type,
    e.profile,
    e.mention_count,
    e.first_mentioned_at,
    e.last_mentioned_at,
    e.access_count,
    e.last_accessed_at,
    e.decay_score,
    e.momentum_score,
    e.surface_score,
    e.centrality_score,
    -- True if this entity is the tenant's self-entity
    (SELECT t.self_entity_id = e.entity_id
       FROM tenants t
      WHERE t.tenant_id = e.tenant_id) AS is_self
  FROM entities e
 WHERE e.merged_into_entity_id IS NULL;       -- canonical only

-- v_relations: subject/object resolved through merge chains. Single
-- source doc per relation (relations.doc_id is singular, not array).
CREATE OR REPLACE VIEW v_relations AS
SELECT
    r.tenant_id,
    r.relation_id,
    r.subject_entity_id,
    r.predicate,
    r.predicate_canonical,    -- nullable; null when predicate_kind='open'
    r.predicate_kind,
    r.object_entity_id,
    r.object_value,
    r.claim_text,
    r.confidence,
    r.valid_time,
    r.archived_at,
    r.superseded_by,
    r.doc_id          AS source_doc_id,
    r.chunk_id        AS source_chunk_id,
    r.created_at
  FROM relations r;

-- v_timeline: chronological event log. Single doc/entity/idea/task ID
-- columns (no aggregated arrays — base table has them as separate
-- columns, only one populated per event).
CREATE OR REPLACE VIEW v_timeline AS
SELECT
    t.tenant_id,
    t.event_id,
    t.event_type     AS kind,
    t.occurred_at,
    t.valid_time,
    t.doc_id,
    t.chunk_id,
    t.entity_id,
    t.idea_id,
    t.task_id,
    t.scope_id,
    t.conflict_id,
    t.actor,
    t.payload         -- jsonb; resurfacing reads any kind-specific fields here
  FROM timeline_events t;

-- v_tasks: tasks + deadlines with overdue computation.
CREATE OR REPLACE VIEW v_tasks AS
SELECT
    t.tenant_id,
    t.task_id,
    t.title,
    t.description,
    t.kind,
    t.status,
    t.priority,
    t.due_at,
    t.time_window,
    (t.due_at IS NOT NULL
       AND t.due_at < now()
       AND t.status NOT IN ('done', 'cancelled'))  AS is_overdue,
    t.owner_entity_id,
    t.related_idea_id,
    t.related_entity_ids,
    t.doc_id          AS source_doc_id,
    t.created_at,
    t.updated_at,
    t.completed_at,
    t.cancelled_at,
    t.access_count,
    t.last_accessed_at
  FROM tasks_deadlines t;

-- v_ideas: ideas with status + access + ownership signals.
CREATE OR REPLACE VIEW v_ideas AS
SELECT
    i.tenant_id,
    i.idea_id,
    i.title,
    i.summary,
    i.category,
    i.status,
    i.priority,
    i.owner_entity_id,
    i.related_entity_ids,
    i.related_idea_ids,
    i.related_task_ids,
    i.doc_id          AS source_doc_id,
    i.parent_idea_id,
    i.cluster_id,
    i.decay_score,
    i.momentum_score,
    i.surface_score,
    i.mention_count,
    i.created_at,
    i.updated_at,
    i.originated_at,
    i.last_revisited_at,
    i.access_count,
    i.last_accessed_at
  FROM ideas i;

-- v_conflicts_open: only currently-unresolved conflicts (the ones the
-- resurfacing layer surfaces for attention).
CREATE OR REPLACE VIEW v_conflicts_open AS
SELECT
    c.tenant_id,
    c.conflict_id,
    c.type             AS kind,
    c.severity,
    c.subject_entity_ids,
    c.predicate,
    c.mention_refs,
    c.doc_refs,
    c.notes,
    c.detected_at,
    c.created_at,
    c.updated_at
  FROM conflicts c
 WHERE c.resolved_state = 'open';

-- v_scope_activity: time-windowed activity per scope. Used by the
-- thinking brain to identify recently-touched scopes.
CREATE OR REPLACE VIEW v_scope_activity AS
SELECT
    s.tenant_id,
    s.scope_id,
    s.slug,
    s.name,
    s.description,
    -- Doc counts per recency window
    COUNT(DISTINCT CASE
      WHEN d.last_accessed_at > now() - interval '24 hours'
        OR d.created_at        > now() - interval '24 hours'
      THEN ds.doc_id
    END) AS doc_count_24h,
    COUNT(DISTINCT CASE
      WHEN d.last_accessed_at > now() - interval '7 days'
        OR d.created_at        > now() - interval '7 days'
      THEN ds.doc_id
    END) AS doc_count_7d,
    MAX(GREATEST(d.last_accessed_at, d.created_at)) AS last_touched_at
  FROM scopes s
  LEFT JOIN doc_scopes ds ON ds.tenant_id = s.tenant_id
                          AND ds.scope_id = s.scope_id
  LEFT JOIN documents  d  ON d.tenant_id  = ds.tenant_id
                          AND d.doc_id    = ds.doc_id
                          AND d.deleted_at IS NULL
 GROUP BY s.tenant_id, s.scope_id, s.slug, s.name, s.description;

-- v_mention_velocity: MATERIALIZED for performance (slope computation
-- on every query is expensive). Refreshed periodically by the dreaming
-- scheduler. Default refresh: every 30 minutes during active hours.
--
-- Slope is normalized into [-1, +1]:
--   positive = mentions in last 24h exceed the 30-day baseline rate
--   negative = entity has gone quiet relative to its baseline
CREATE MATERIALIZED VIEW v_mention_velocity AS
WITH per_entity AS (
  SELECT
    e.tenant_id,
    e.entity_id,
    e.canonical_name AS name,
    (SELECT count(*) FROM relations r
       WHERE r.tenant_id = e.tenant_id
         AND (r.subject_entity_id = e.entity_id OR r.object_entity_id = e.entity_id)
         AND r.created_at > now() - interval '24 hours') AS m24,
    (SELECT count(*) FROM relations r
       WHERE r.tenant_id = e.tenant_id
         AND (r.subject_entity_id = e.entity_id OR r.object_entity_id = e.entity_id)
         AND r.created_at > now() - interval '7 days')   AS m7d,
    (SELECT count(*) FROM relations r
       WHERE r.tenant_id = e.tenant_id
         AND (r.subject_entity_id = e.entity_id OR r.object_entity_id = e.entity_id)
         AND r.created_at > now() - interval '30 days')  AS m30d
  FROM entities e
 WHERE e.merged_into_entity_id IS NULL
)
SELECT
  tenant_id,
  entity_id,
  name,
  m24  AS mentions_24h,
  m7d  AS mentions_7d,
  m30d AS mentions_30d,
  -- Slope normalized to [-1, +1]
  CASE
    WHEN m30d = 0 THEN 0::double precision
    ELSE LEAST(
      1.0,
      GREATEST(
        -1.0,
        ( (m24::double precision)
          - (m7d::double precision / 7.0) )
        / GREATEST(m30d::double precision / 30.0, 0.001)
      )
    )
  END AS slope_24h_vs_baseline,
  now() AS computed_at
FROM per_entity;

CREATE UNIQUE INDEX ix_mention_velocity_pk
  ON v_mention_velocity (tenant_id, entity_id);

CREATE INDEX ix_mention_velocity_slope
  ON v_mention_velocity (tenant_id, slope_24h_vs_baseline DESC);

COMMENT ON MATERIALIZED VIEW v_mention_velocity IS
  'Per-entity mention velocity. WARNING: callers MUST filter by tenant_id explicitly; '
  'materialized views do not enforce RLS. Wrap in SECURITY INVOKER view in v0.2.';


-- ─────────────────────────────────────────────────────────────────────────
-- Section B — Resurfacing-owned tables
-- ─────────────────────────────────────────────────────────────────────────

-- B.1 — resurface_observations: every agent interaction lands here.
-- Used by the tool-use pattern engine + behavioral learning. tool_input
-- often contains the user's query text — encrypted at the app layer via
-- the existing envelope pattern. agent_id is registered up-front; the API
-- verifies the agent's tenant-issued token before accepting writes.
CREATE TABLE resurface_observations (
  observation_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  agent_id        text NOT NULL,
  observed_at     timestamptz NOT NULL DEFAULT now(),
  context         jsonb NOT NULL,
  tool_called     text NOT NULL,
  tool_input      jsonb,                    -- encrypted at app layer
  outcome         jsonb NOT NULL,
  CONSTRAINT chk_obs_context_is_object CHECK (jsonb_typeof(context) = 'object'),
  CONSTRAINT chk_obs_outcome_is_object CHECK (jsonb_typeof(outcome) = 'object')
);
COMMENT ON TABLE resurface_observations IS
  'Phase 5a — Tool-use pattern engine event log. tool_input encrypted at app layer.';

CREATE INDEX ix_resurface_obs_tenant_time
  ON resurface_observations (tenant_id, observed_at DESC);
CREATE INDEX ix_resurface_obs_tool
  ON resurface_observations (tenant_id, tool_called, observed_at DESC);
CREATE INDEX ix_resurface_obs_agent
  ON resurface_observations (tenant_id, agent_id, observed_at DESC);

-- B.2 — resurface_artifacts: dreaming output, TTL-bound.
CREATE TYPE resurface_artifact_kind AS ENUM (
  'predicted_question',
  'cross_doc_relation',
  'conflict_candidate',
  'topic_cluster',
  'recall_cache'
);

CREATE TABLE resurface_artifacts (
  artifact_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  kind                 resurface_artifact_kind NOT NULL,
  content              jsonb NOT NULL,            -- shape varies by kind; encrypted at app layer
  related_chunk_ids    uuid[] NOT NULL DEFAULT '{}',
  related_entity_ids   uuid[] NOT NULL DEFAULT '{}',
  generated_at         timestamptz NOT NULL DEFAULT now(),
  expires_at           timestamptz,               -- nullable; NULL = no auto-expiry
  dreaming_run_id      uuid,                      -- FK below
  cloud_egress_used    boolean NOT NULL DEFAULT false,
  CONSTRAINT chk_artifact_content_is_object CHECK (jsonb_typeof(content) = 'object')
);
COMMENT ON TABLE resurface_artifacts IS
  'Phase 5a — Dreaming output. Reaper deletes WHERE expires_at < now() daily.';

CREATE INDEX ix_resurface_artifacts_tenant_kind
  ON resurface_artifacts (tenant_id, kind, generated_at DESC);
CREATE INDEX ix_resurface_artifacts_expires
  ON resurface_artifacts (expires_at)
  WHERE expires_at IS NOT NULL;
CREATE INDEX ix_resurface_artifacts_chunks
  ON resurface_artifacts USING gin (related_chunk_ids);
CREATE INDEX ix_resurface_artifacts_entities
  ON resurface_artifacts USING gin (related_entity_ids);

-- B.3 — resurface_dreaming_runs: bookkeeping for scheduled + event
-- triggers. trigger_meta carries trigger-specific context.
CREATE TYPE resurface_run_trigger AS ENUM (
  'scheduled',
  'event_idle',
  'event_burst',
  'event_query',
  'manual'
);
CREATE TYPE resurface_run_status AS ENUM (
  'running',
  'succeeded',
  'failed',
  'cancelled'
);

CREATE TABLE resurface_dreaming_runs (
  run_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  trigger             resurface_run_trigger NOT NULL,
  trigger_meta        jsonb,
  started_at          timestamptz NOT NULL DEFAULT now(),
  ended_at            timestamptz,
  status              resurface_run_status NOT NULL DEFAULT 'running',
  artifacts_created   integer NOT NULL DEFAULT 0,
  cloud_egress_used   boolean NOT NULL DEFAULT false,
  cloud_tokens_used   integer NOT NULL DEFAULT 0,
  error_reason        text
);
COMMENT ON TABLE resurface_dreaming_runs IS
  'Phase 5a — Dreaming run audit. Cloud token accounting + budget tracking.';

CREATE INDEX ix_dreaming_runs_tenant_started
  ON resurface_dreaming_runs (tenant_id, started_at DESC);
CREATE INDEX ix_dreaming_runs_status
  ON resurface_dreaming_runs (tenant_id, status, started_at DESC);

-- Stitch artifacts → run FK (declared after both tables exist).
ALTER TABLE resurface_artifacts
  ADD CONSTRAINT fk_artifacts_dreaming_run
    FOREIGN KEY (dreaming_run_id)
    REFERENCES resurface_dreaming_runs(run_id)
    ON DELETE SET NULL;

-- B.4 — resurface_patterns_cache: pre-computed signals.
-- One row per (kind, subject_kind, subject_id). UNIQUE on the four-tuple
-- means trending + attention can co-exist as separate rows for the same
-- subject (they're separate kinds; both can fire).
CREATE TYPE resurface_pattern_kind AS ENUM (
  'trending',     -- mention frequency rising
  'fading',       -- mention frequency declining
  'forgotten',    -- created but never engaged with
  'recurring',    -- regular cadence detected
  'attention'     -- composite signal: needs user attention (separable from trending)
);
CREATE TYPE resurface_subject_kind AS ENUM (
  'entity', 'idea', 'task', 'topic', 'scope'
);

CREATE TABLE resurface_patterns_cache (
  pattern_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  kind            resurface_pattern_kind NOT NULL,
  subject_kind    resurface_subject_kind NOT NULL,
  subject_id      uuid NOT NULL,
  score           double precision NOT NULL,
  reason          text NOT NULL,
  computed_at     timestamptz NOT NULL DEFAULT now(),
  ttl_at          timestamptz NOT NULL,
  CONSTRAINT uq_patterns_one_per_kind_subject
    UNIQUE (tenant_id, kind, subject_kind, subject_id)
);
COMMENT ON TABLE resurface_patterns_cache IS
  'Phase 5a — Pre-computed pattern signals. UNIQUE per (kind, subject) means '
  'attention + trending can co-exist as separate rows for the same subject.';

CREATE INDEX ix_patterns_tenant_kind_score
  ON resurface_patterns_cache (tenant_id, kind, score DESC);
CREATE INDEX ix_patterns_ttl
  ON resurface_patterns_cache (ttl_at);
CREATE INDEX ix_patterns_subject
  ON resurface_patterns_cache (tenant_id, subject_kind, subject_id);

-- B.5 — resurface_user_preferences: learned per-tenant agent-tuning.
CREATE TABLE resurface_user_preferences (
  tenant_id                       uuid PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  answer_length_preference        text NOT NULL DEFAULT 'normal',
  citation_density_preference     text NOT NULL DEFAULT 'normal',
  persona_tone                    jsonb NOT NULL DEFAULT '{}',
  tool_use_preferences            jsonb NOT NULL DEFAULT '{}',
  surfacing_aggressiveness        text NOT NULL DEFAULT 'normal',
  updated_at                      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chk_pref_answer_len  CHECK (answer_length_preference IN ('concise','normal','exhaustive')),
  CONSTRAINT chk_pref_cite_dens   CHECK (citation_density_preference IN ('low','normal','high')),
  CONSTRAINT chk_pref_aggressive  CHECK (surfacing_aggressiveness IN ('conservative','normal','aggressive'))
);
COMMENT ON TABLE resurface_user_preferences IS
  'Phase 5a — Per-tenant agent-tunable preferences. Updated by the tool-use '
  'pattern engine when it detects a clear signal from observations.';


-- ─────────────────────────────────────────────────────────────────────────
-- Section C — Tenant flags for resurfacing-aware cloud-policy + budget
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE tenants
  ADD COLUMN allow_cloud_for_background_jobs boolean NOT NULL DEFAULT false,
  ADD COLUMN budget_caps                     jsonb   NOT NULL DEFAULT '{}';
COMMENT ON COLUMN tenants.allow_cloud_for_background_jobs IS
  'Independent of allow_cloud_for_personal_data. Controls dreaming + scheduled '
  'jobs. Default false to avoid bill shock from background cloud egress.';
COMMENT ON COLUMN tenants.budget_caps IS
  'Per-month + per-agent token budgets. Shape: '
  '{monthly_total: int, per_agent: {agent_id: int}, code_ceiling: int}. '
  'Resurfacing budget controller reads this; degrades gracefully near limits.';


-- ─────────────────────────────────────────────────────────────────────────
-- Section D — RLS application
-- ─────────────────────────────────────────────────────────────────────────
SELECT woxpas_apply_tenant_rls('resurface_observations'::regclass);
SELECT woxpas_apply_tenant_rls('resurface_artifacts'::regclass);
SELECT woxpas_apply_tenant_rls('resurface_dreaming_runs'::regclass);
SELECT woxpas_apply_tenant_rls('resurface_patterns_cache'::regclass);
SELECT woxpas_apply_tenant_rls('resurface_user_preferences'::regclass);

INSERT INTO schema_migrations (version) VALUES ('022_resurfacing_foundation');

COMMIT;
