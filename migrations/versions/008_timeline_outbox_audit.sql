-- 008 — Timeline events, transactional outbox, audit log, surfacing feedback, pipeline runs
--
-- These are the low-level substrates. Timeline events power surfacing intelligence;
-- outbox decouples pipelines via durable events; audit captures every privileged action.

-- ─── Timeline events (append-only fact stream) ────────────────────────────
-- Every reference to any addressable item writes one row. The surfacing engine reads
-- this stream to compute decay, momentum, co-occurrence — orders of magnitude cheaper
-- than scanning chunks/mentions.
CREATE TABLE timeline_events (
  event_id      bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  occurred_at   timestamptz NOT NULL DEFAULT now(),          -- system time
  valid_time    timestamptz,                                 -- when the content is "about" (if known)

  event_type    text NOT NULL
                 CHECK (event_type IN (
                   'doc.ingested','doc.summarized','doc.indexed','doc.accessed',
                   'mention.created','mention.updated',
                   'idea.created','idea.status_changed','idea.revisited',
                   'task.created','task.completed','task.scheduled',
                   'entity.created','entity.merged',
                   'conflict.detected','conflict.resolved',
                   'retrieval.hit','surface.shown','surface.acted','surface.dismissed','surface.snoozed'
                 )),

  -- one of these is set depending on event_type
  doc_id        uuid,
  chunk_id      uuid,
  span_id       uuid,
  block_id      uuid,
  entity_id     uuid,
  idea_id       uuid,
  task_id       uuid,
  scope_id      uuid,
  conflict_id   uuid,

  actor         text,                                        -- user | agent | system | extractor
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX timeline_tenant_time_idx     ON timeline_events(tenant_id, occurred_at DESC);
CREATE INDEX timeline_type_time_idx       ON timeline_events(tenant_id, event_type, occurred_at DESC);
CREATE INDEX timeline_entity_idx          ON timeline_events(entity_id, occurred_at DESC) WHERE entity_id IS NOT NULL;
CREATE INDEX timeline_doc_idx             ON timeline_events(doc_id, occurred_at DESC)    WHERE doc_id IS NOT NULL;
CREATE INDEX timeline_idea_idx            ON timeline_events(idea_id, occurred_at DESC)   WHERE idea_id IS NOT NULL;
CREATE INDEX timeline_task_idx            ON timeline_events(task_id, occurred_at DESC)   WHERE task_id IS NOT NULL;
CREATE INDEX timeline_scope_idx           ON timeline_events(scope_id, occurred_at DESC)  WHERE scope_id IS NOT NULL;
CREATE INDEX timeline_valid_time_idx      ON timeline_events(tenant_id, valid_time)        WHERE valid_time IS NOT NULL;

-- ─── Transactional outbox (event bus) ─────────────────────────────────────
-- Pipelines write here in the same tx as state changes. A publisher daemon reads
-- and dispatches via Postgres LISTEN/NOTIFY locally (NATS/Kafka in cloud lift).
-- Events are forward-compatible (additive only); consumers pin a version range.
CREATE TABLE events_outbox (
  outbox_id        bigserial PRIMARY KEY,
  tenant_id        uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  event_type       text NOT NULL,                            -- e.g., document.ingested, document.summarized
  event_version    text NOT NULL DEFAULT '1',
  pipeline_version text NOT NULL,
  aggregate_kind   text NOT NULL,                            -- document | task | idea | entity | conflict
  aggregate_id     uuid NOT NULL,
  payload          jsonb NOT NULL,
  causation_id     uuid,                                     -- previous outbox event in the chain
  correlation_id   uuid,                                     -- trace id

  created_at       timestamptz NOT NULL DEFAULT now(),
  scheduled_for    timestamptz NOT NULL DEFAULT now(),       -- delayed delivery support
  available_after  timestamptz NOT NULL DEFAULT now(),       -- visibility timeout for retry
  attempts         int NOT NULL DEFAULT 0,
  max_attempts     int NOT NULL DEFAULT 8,

  published_at     timestamptz,
  failed_at        timestamptz,
  last_error       text
);
CREATE INDEX outbox_pending_idx ON events_outbox(scheduled_for, available_after)
                                  WHERE published_at IS NULL AND failed_at IS NULL;
CREATE INDEX outbox_aggregate_idx ON events_outbox(aggregate_kind, aggregate_id);
CREATE INDEX outbox_tenant_idx    ON events_outbox(tenant_id, created_at DESC);

-- LISTEN/NOTIFY trigger for low-latency local dispatch.
CREATE OR REPLACE FUNCTION outbox_notify() RETURNS trigger
  LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('woxpas_outbox', NEW.outbox_id::text);
  RETURN NEW;
END$$;
CREATE TRIGGER outbox_notify_trigger AFTER INSERT ON events_outbox
  FOR EACH ROW EXECUTE FUNCTION outbox_notify();

-- ─── Pipeline runs (per-stage execution tracking) ─────────────────────────
CREATE TABLE pipeline_runs (
  run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  doc_id            uuid REFERENCES documents(doc_id) ON DELETE CASCADE,

  stage             text NOT NULL
                     CHECK (stage IN (
                       'ingress','av_scan','type_detect','extract','normalize',
                       'summarize','chunk','embed','index','classify','dispatch'
                     )),
  status            text NOT NULL
                     CHECK (status IN ('pending','running','succeeded','failed','skipped','cancelled')),
  pipeline_version  text NOT NULL,

  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  duration_ms       int GENERATED ALWAYS AS
                     (CASE WHEN finished_at IS NOT NULL
                           THEN extract(epoch from (finished_at - started_at))::int * 1000
                           ELSE NULL END) STORED,

  error_class       text,
  error_message     text,
  error_retriable   boolean,
  attempt           int NOT NULL DEFAULT 1,

  metrics           jsonb NOT NULL DEFAULT '{}'::jsonb       -- {chunks_produced, tokens_embedded, ms_per_chunk, ...}
);
CREATE INDEX pipeline_runs_doc_idx       ON pipeline_runs(doc_id, started_at DESC);
CREATE INDEX pipeline_runs_stage_idx     ON pipeline_runs(stage, status, started_at DESC);
CREATE INDEX pipeline_runs_failed_idx    ON pipeline_runs(tenant_id, finished_at DESC) WHERE status = 'failed';

-- ─── Audit log (append-only) ──────────────────────────────────────────────
CREATE TABLE audit_log (
  audit_id      bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  actor         text NOT NULL,                                -- user_id | agent_id | system | service:<name>
  actor_kind    text NOT NULL CHECK (actor_kind IN ('user','agent','system','service')),
  action        text NOT NULL,                                -- e.g., document.read, scope.create, session.unlock
  target_kind   text,
  target_id     uuid,
  outcome       text NOT NULL DEFAULT 'success'
                  CHECK (outcome IN ('success','denied','error')),
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  client        jsonb                                         -- {ip?, user_agent?, fingerprint}
);
CREATE INDEX audit_tenant_time_idx ON audit_log(tenant_id, occurred_at DESC);
CREATE INDEX audit_actor_idx       ON audit_log(actor, occurred_at DESC);
CREATE INDEX audit_target_idx      ON audit_log(target_kind, target_id) WHERE target_id IS NOT NULL;

-- Audit log is append-only at the role level (grants in 009).

-- ─── Surfacing feedback ──────────────────────────────────────────────────
CREATE TABLE surfacing_feedback (
  feedback_id    bigserial PRIMARY KEY,
  tenant_id      uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  surfaced_kind  text NOT NULL CHECK (surfaced_kind IN ('document','entity','idea','task','conflict','scope')),
  surfaced_id    uuid NOT NULL,

  reaction       text NOT NULL
                  CHECK (reaction IN ('shown','dismissed','opened','acted','snoozed','helpful','not_helpful')),
  reason         text,
  context        jsonb NOT NULL DEFAULT '{}'::jsonb,          -- {trigger, rule_id, surface_score, ...}

  occurred_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX surfacing_feedback_target_idx ON surfacing_feedback(surfaced_kind, surfaced_id, occurred_at DESC);
CREATE INDEX surfacing_feedback_tenant_idx ON surfacing_feedback(tenant_id, occurred_at DESC);

-- ─── Surfacing rules (declarative triggers; deferred runtime) ────────────
CREATE TABLE surfacing_rules (
  rule_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  name         text NOT NULL,
  description  text,
  trigger_kind text NOT NULL
                 CHECK (trigger_kind IN ('cron','calendar_anchor','momentum','recency','co_occurrence','custom')),
  trigger_def  jsonb NOT NULL,
  target_kind  text NOT NULL,                                 -- what this rule surfaces
  active       boolean NOT NULL DEFAULT true,
  priority     int NOT NULL DEFAULT 100,
  created_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('008_timeline_outbox_audit');
