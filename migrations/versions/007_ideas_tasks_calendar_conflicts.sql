-- 007 — Ideas, tasks (calendar-shaped), calendar events, conflicts
--
-- All populated by the EXTRACTION pipeline (separate build).
-- Ingestion creates the slots so contracts are stable from day one.

-- ─── Ideas ────────────────────────────────────────────────────────────────
-- An idea is a candidate-for-action insight extracted from a thought/doc.
-- Agents/swarms read this table to identify what to act on.

CREATE TABLE idea_clusters (
  cluster_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  label          text,
  description    text,
  centroid_ref   uuid,                                       -- Qdrant point id
  member_count   int NOT NULL DEFAULT 0,
  decay_score    real NOT NULL DEFAULT 1.0,
  momentum_score real NOT NULL DEFAULT 0.0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ideas (
  idea_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  -- provenance
  doc_id         uuid REFERENCES documents(doc_id) ON DELETE CASCADE,
  block_id       uuid REFERENCES canonical_blocks(block_id) ON DELETE SET NULL,
  span_id        uuid REFERENCES spans(span_id) ON DELETE SET NULL,
  chunk_id       uuid REFERENCES chunks(chunk_id) ON DELETE SET NULL,

  -- content
  title          text,
  summary        text NOT NULL,
  embedding_ref  uuid,
  cluster_id     uuid REFERENCES idea_clusters(cluster_id) ON DELETE SET NULL,
  parent_idea_id uuid REFERENCES ideas(idea_id) ON DELETE SET NULL,

  -- status
  status         text NOT NULL DEFAULT 'raw'
                  CHECK (status IN ('raw','developing','active','dormant','abandoned','realized')),
  category       text,                                       -- product | technical | personal | ...
  priority       text DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent')),

  -- relations (denormalized for fast read)
  related_entity_ids  uuid[] DEFAULT '{}',
  related_idea_ids    uuid[] DEFAULT '{}',
  related_task_ids    uuid[] DEFAULT '{}',

  -- surfacing
  decay_score    real NOT NULL DEFAULT 1.0,
  momentum_score real NOT NULL DEFAULT 0.0,
  surface_score  real NOT NULL DEFAULT 0.0,
  mention_count  int  NOT NULL DEFAULT 1,

  -- bitemporal
  originated_at  timestamptz NOT NULL DEFAULT now(),
  last_revisited_at timestamptz,
  system_time    tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time     tstzrange,

  confidence     real,
  extractor      text,
  extractor_version text,

  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ideas_tenant_status_idx   ON ideas(tenant_id, status);
CREATE INDEX ideas_cluster_idx         ON ideas(cluster_id) WHERE cluster_id IS NOT NULL;
CREATE INDEX ideas_doc_idx             ON ideas(doc_id) WHERE doc_id IS NOT NULL;
CREATE INDEX ideas_decay_idx           ON ideas(tenant_id, decay_score DESC) WHERE status NOT IN ('abandoned','realized');
CREATE INDEX ideas_momentum_idx        ON ideas(tenant_id, momentum_score DESC) WHERE status NOT IN ('abandoned','realized');
CREATE INDEX ideas_related_entities_gin ON ideas USING gin (related_entity_ids);

-- ─── Tasks (calendar-shaped) ──────────────────────────────────────────────
-- One table covers tasks (no time), scheduled tasks (has time_window), and
-- task-derived calendar items. Calendar events imported as docs link via task_id?.

CREATE TABLE tasks_deadlines (
  task_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  -- provenance (NULL when created directly by user/agent without doc origin)
  doc_id             uuid REFERENCES documents(doc_id) ON DELETE SET NULL,
  block_id           uuid REFERENCES canonical_blocks(block_id) ON DELETE SET NULL,
  span_id            uuid REFERENCES spans(span_id) ON DELETE SET NULL,
  chunk_id           uuid REFERENCES chunks(chunk_id) ON DELETE SET NULL,

  -- content
  title              text NOT NULL,
  description        text,
  kind               text NOT NULL DEFAULT 'task'
                       CHECK (kind IN ('task','event','reminder','deadline')),
  status             text NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','scheduled','in_progress','blocked','done','cancelled','snoozed')),
  priority           text DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent')),

  -- when
  due_at             timestamptz,
  time_window        tstzrange,
  timezone           text,
  all_day            boolean NOT NULL DEFAULT false,
  recurrence_rule    text,                                   -- RFC 5545 RRULE
  recurrence_exdate  timestamptz[],
  recurrence_parent_task_id uuid REFERENCES tasks_deadlines(task_id) ON DELETE CASCADE,

  -- external sync (calendar bidirectional)
  external_refs      jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- [{system: 'gcal', account_id, calendar_id, event_id, etag, last_pulled_at, last_pushed_at}]
  sync_state         text NOT NULL DEFAULT 'local_only'
                       CHECK (sync_state IN ('local_only','clean','dirty','conflicted')),
  sync_dirty_at      timestamptz,
  sync_error         jsonb,

  -- relations (denormalized for fast read)
  related_idea_id    uuid REFERENCES ideas(idea_id) ON DELETE SET NULL,
  related_entity_ids uuid[] DEFAULT '{}',

  -- bitemporal
  system_time        tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  valid_time         tstzrange,

  -- attendees (for event kind)
  attendees          jsonb DEFAULT '[]'::jsonb,
  location           text,

  confidence         real,
  extractor          text,
  extractor_version  text,
  created_by         text NOT NULL DEFAULT 'extraction'
                       CHECK (created_by IN ('user','agent','extraction','sync','rule')),

  completed_at       timestamptz,
  cancelled_at       timestamptz,

  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tasks_tenant_status_idx       ON tasks_deadlines(tenant_id, status)
                                             WHERE status NOT IN ('done','cancelled');
CREATE INDEX tasks_due_idx                 ON tasks_deadlines(tenant_id, due_at)
                                             WHERE due_at IS NOT NULL AND status NOT IN ('done','cancelled');
CREATE INDEX tasks_time_window_gist        ON tasks_deadlines USING gist (time_window)
                                             WHERE time_window IS NOT NULL;
CREATE INDEX tasks_sync_dirty_idx          ON tasks_deadlines(tenant_id, sync_dirty_at)
                                             WHERE sync_state = 'dirty';
CREATE INDEX tasks_recurrence_parent_idx   ON tasks_deadlines(recurrence_parent_task_id)
                                             WHERE recurrence_parent_task_id IS NOT NULL;
CREATE INDEX tasks_related_idea_idx        ON tasks_deadlines(related_idea_id)
                                             WHERE related_idea_id IS NOT NULL;
CREATE INDEX tasks_doc_idx                 ON tasks_deadlines(doc_id) WHERE doc_id IS NOT NULL;
CREATE INDEX tasks_external_refs_gin       ON tasks_deadlines USING gin (external_refs jsonb_path_ops);

-- ─── Calendar events as ingestion source ──────────────────────────────────
-- When a calendar is connected, each event is ingested as a document
-- (processing_profile = 'calendar_event'). This table lets the agent query
-- "what's on my calendar" without scanning documents.
CREATE TABLE calendar_events (
  calendar_event_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  doc_id             uuid NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  source_id          uuid REFERENCES sources(source_id) ON DELETE SET NULL,

  external_calendar_id text,
  external_event_id    text,
  etag                 text,

  title              text NOT NULL,
  description        text,
  location           text,
  attendees          jsonb DEFAULT '[]'::jsonb,
  organizer          jsonb,

  time_window        tstzrange NOT NULL,
  timezone           text,
  all_day            boolean NOT NULL DEFAULT false,
  recurrence_rule    text,

  status             text NOT NULL DEFAULT 'confirmed'
                       CHECK (status IN ('confirmed','tentative','cancelled')),
  related_task_id    uuid REFERENCES tasks_deadlines(task_id) ON DELETE SET NULL,

  last_pulled_at     timestamptz,
  last_pushed_at     timestamptz,

  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT calendar_events_external_unique UNIQUE (tenant_id, external_calendar_id, external_event_id)
);
CREATE INDEX calendar_events_window_gist   ON calendar_events USING gist (time_window);
CREATE INDEX calendar_events_doc_idx       ON calendar_events(doc_id);
CREATE INDEX calendar_events_related_task  ON calendar_events(related_task_id)
                                             WHERE related_task_id IS NOT NULL;

-- ─── Conflicts (typed) ─────────────────────────────────────────────────────
-- Type 1 (file-level supersession), Type 2 sub-types (value, knowledge, temporal,
-- opinion), and timeslot conflicts (overlapping calendar items).
CREATE TABLE conflicts (
  conflict_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  type               text NOT NULL
                       CHECK (type IN (
                         'supersession',         -- Type 1: revision of own thought
                         'value',                -- numeric/literal disagreement (price, date, count)
                         'knowledge',            -- categorical disagreement
                         'temporal',             -- was-true-now-false (resolved by valid_time)
                         'opinion',              -- subjective disagreement (intentional)
                         'timeslot',             -- calendar overlap
                         'reference'             -- doc cites contradicting source
                       )),
  severity           text NOT NULL DEFAULT 'medium'
                       CHECK (severity IN ('low','medium','high','critical')),

  -- subjects/predicate
  subject_entity_ids uuid[] NOT NULL DEFAULT '{}',
  predicate          text,

  -- the contradicting facts
  mention_refs       jsonb NOT NULL,                         -- [{relation_id|mention_id|task_id|doc_id, value, valid_time}]
  doc_refs           uuid[] NOT NULL DEFAULT '{}',

  -- resolution
  resolved_state     text NOT NULL DEFAULT 'open'
                       CHECK (resolved_state IN ('open','auto_resolved','human_resolved','living','dismissed')),
  resolution_method  text,                                   -- recency | manual | rule | merge
  resolved_value     jsonb,
  resolved_by        text,                                   -- user | agent | system
  resolved_at        timestamptz,

  notes              text,

  detected_at        timestamptz NOT NULL DEFAULT now(),
  detector           text,
  detector_version   text,

  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conflicts_tenant_type_idx     ON conflicts(tenant_id, type, resolved_state);
CREATE INDEX conflicts_subjects_gin        ON conflicts USING gin (subject_entity_ids);
CREATE INDEX conflicts_open_idx            ON conflicts(tenant_id, severity, detected_at DESC)
                                             WHERE resolved_state = 'open';
CREATE INDEX conflicts_doc_refs_gin        ON conflicts USING gin (doc_refs);

INSERT INTO schema_migrations (version) VALUES ('007_ideas_tasks_calendar_conflicts');
