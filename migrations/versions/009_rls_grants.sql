-- 009 — Row-level security + grants
--
-- Every tenant-scoped table enables RLS keyed on:
--   current_setting('app.tenant_id')::uuid = tenant_id
-- The application sets app.tenant_id at session start (after JWT verification).
-- Bypassing requires the migration role; the runtime woxpas_app role is FORCE'd.

-- ─── Helper: apply standard tenant isolation policy ───────────────────────
CREATE OR REPLACE FUNCTION woxpas_apply_tenant_rls(target_table regclass) RETURNS void
  LANGUAGE plpgsql AS $$
DECLARE
  policy_name text := format('%s_tenant_isolation', target_table::text);
BEGIN
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target_table);
  EXECUTE format('ALTER TABLE %s FORCE  ROW LEVEL SECURITY', target_table);
  EXECUTE format(
    'CREATE POLICY %I ON %s
       USING       (tenant_id = current_setting(''app.tenant_id'', true)::uuid)
       WITH CHECK  (tenant_id = current_setting(''app.tenant_id'', true)::uuid)',
    policy_name, target_table
  );
END$$;

SELECT woxpas_apply_tenant_rls('tenants'::regclass);
SELECT woxpas_apply_tenant_rls('tenant_session_log'::regclass);
SELECT woxpas_apply_tenant_rls('sources'::regclass);
SELECT woxpas_apply_tenant_rls('source_credentials'::regclass);
SELECT woxpas_apply_tenant_rls('scopes'::regclass);
SELECT woxpas_apply_tenant_rls('doc_scopes'::regclass);
SELECT woxpas_apply_tenant_rls('scope_rules'::regclass);
SELECT woxpas_apply_tenant_rls('documents'::regclass);
SELECT woxpas_apply_tenant_rls('canonical_blocks'::regclass);
SELECT woxpas_apply_tenant_rls('chunks'::regclass);
SELECT woxpas_apply_tenant_rls('spans'::regclass);
SELECT woxpas_apply_tenant_rls('entities'::regclass);
SELECT woxpas_apply_tenant_rls('entity_aliases'::regclass);
SELECT woxpas_apply_tenant_rls('entity_mentions'::regclass);
SELECT woxpas_apply_tenant_rls('relations'::regclass);
SELECT woxpas_apply_tenant_rls('idea_clusters'::regclass);
SELECT woxpas_apply_tenant_rls('ideas'::regclass);
SELECT woxpas_apply_tenant_rls('tasks_deadlines'::regclass);
SELECT woxpas_apply_tenant_rls('calendar_events'::regclass);
SELECT woxpas_apply_tenant_rls('conflicts'::regclass);
SELECT woxpas_apply_tenant_rls('timeline_events'::regclass);
SELECT woxpas_apply_tenant_rls('events_outbox'::regclass);
SELECT woxpas_apply_tenant_rls('pipeline_runs'::regclass);
SELECT woxpas_apply_tenant_rls('audit_log'::regclass);
SELECT woxpas_apply_tenant_rls('surfacing_feedback'::regclass);
SELECT woxpas_apply_tenant_rls('surfacing_rules'::regclass);

-- ─── Grants ──────────────────────────────────────────────────────────────
-- woxpas_app: runtime DML role used by API + workers
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO woxpas_app;
GRANT USAGE,   SELECT, UPDATE        ON ALL SEQUENCES IN SCHEMA public TO woxpas_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO woxpas_app;

-- audit_log is append-only for non-superusers
REVOKE UPDATE, DELETE ON audit_log FROM woxpas_app;

-- woxpas_audit: separate role that only writes audit (used by audit-emitter sidecar)
GRANT INSERT ON audit_log                   TO woxpas_audit;
GRANT USAGE,  SELECT ON audit_log_audit_id_seq TO woxpas_audit;

-- Default privileges so future tables inherit
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO woxpas_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO woxpas_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO woxpas_app;

INSERT INTO schema_migrations (version) VALUES ('009_rls_grants');
