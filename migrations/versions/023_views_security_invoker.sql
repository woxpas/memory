-- 023 — CRITICAL FIX: views in 022 were created without security_invoker=true,
-- which means they ran as the OWNER (woxpas, who has rolbypassrls=true)
-- regardless of the querier's role. Result: RLS was bypassed when reading
-- through views, leaking cross-tenant data.
--
-- Postgres 15+ default for CREATE VIEW is `security_invoker = false`. Without
-- explicit setting, views inherit owner privileges. For the resurfacing view
-- contract to actually enforce tenant isolation, every v_* view must be
-- ALTER VIEW ... SET (security_invoker = true).
--
-- Materialized views (v_mention_velocity) DON'T support security_invoker —
-- they refresh as the owner and store rows. Callers MUST filter explicitly
-- by tenant_id (already documented in 022 + enforced in resurfacing.engine).
--
-- After this migration: a query through any v_* view runs as the QUERIER's
-- role (woxpas_app), which has rolbypassrls=false, so RLS policies on the
-- base tables apply. Tested by the same cross-tenant check that initially
-- caught the leak.

BEGIN;

ALTER VIEW v_chunks          SET (security_invoker = true);
ALTER VIEW v_documents       SET (security_invoker = true);
ALTER VIEW v_entities        SET (security_invoker = true);
ALTER VIEW v_relations       SET (security_invoker = true);
ALTER VIEW v_timeline        SET (security_invoker = true);
ALTER VIEW v_tasks           SET (security_invoker = true);
ALTER VIEW v_ideas           SET (security_invoker = true);
ALTER VIEW v_conflicts_open  SET (security_invoker = true);
ALTER VIEW v_scope_activity  SET (security_invoker = true);

-- v_mention_velocity is a MATERIALIZED view — security_invoker does not
-- apply. Refresh runs as owner (which is fine; the refresher has the
-- right scope). Callers MUST filter by tenant_id explicitly. The
-- resurfacing engine does this; document the constraint loud.

INSERT INTO schema_migrations (version) VALUES ('023_views_security_invoker');

COMMIT;
