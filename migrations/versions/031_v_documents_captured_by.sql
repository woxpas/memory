-- 031 — Phase 5a.3.4 Track 2b: expose captured_by on v_documents view.
--
-- The OpportunityDetector's doc_unscoped detector needs to distinguish
-- AGENT-captured vs USER-captured docs to surface them at different
-- urgency. captured_by is already on the base table; this just exposes
-- it through the stable view layer (resurfacing layer's only legal
-- read path per the backend-stability commitment).
--
-- CREATE OR REPLACE preserves any RLS/security_invoker settings.
-- security_invoker explicitly re-set to be safe (Phase 5a.1 fix #023).

BEGIN;

-- CREATE OR REPLACE VIEW only allows ADDITION of new columns at the END
-- of the SELECT list (existing column positions/types must match). New
-- columns therefore appended at end; consumers that don't read them are
-- unaffected.
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
    source_channel
   FROM documents d
  WHERE ((deleted_at IS NULL) AND (archived_at IS NULL));

ALTER VIEW v_documents SET (security_invoker = true);

INSERT INTO schema_migrations (version) VALUES ('031_v_documents_captured_by');

COMMIT;
