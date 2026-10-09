-- 026 — Add archived_at filter to v_documents (Phase 5a.2.2 belt-and-suspenders).
--
-- Currently v_documents filters `deleted_at IS NULL` only. Documents
-- have both `archived_at` and `deleted_at` columns; archived items
-- should not surface in the resurfacing layer (user explicitly archived
-- them — strong negative signal). Archive UX doesn't exist yet in v0.1
-- so this is a no-op today, but future-proofs against the case where
-- archive UI lands and resurfacing accidentally surfaces archived items.
--
-- security_invoker=true preserved (set in migration 023).

BEGIN;

CREATE OR REPLACE VIEW v_documents
WITH (security_invoker = true) AS
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
 WHERE d.deleted_at  IS NULL
   AND d.archived_at IS NULL;             -- belt-and-suspenders

INSERT INTO schema_migrations (version) VALUES ('026_views_archived_filter');

COMMIT;
