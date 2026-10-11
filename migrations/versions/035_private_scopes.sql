-- 035 — Team memory, Phase A4: private scopes
--
-- A scope can be 'team' (default — everyone in the team sees it, unchanged) or
-- 'private' (visible only to its owner_member_id, plus admins/owners). This is
-- the one new access axis inside the tenant boundary; it is enforced by an
-- extra RLS predicate on the `scopes` table and on `doc_scopes` (so a private
-- scope's very membership is hidden from others, and v_documents.scope_slugs
-- won't reveal a private grouping to non-owners).
--
-- SCOPE OF THIS PHASE (read carefully): A4 makes the private SCOPE and its
-- membership labels owner-only. It does NOT yet hide the underlying DOCUMENT
-- rows, chunks, or extracted entities/relations — those remain tenant-visible
-- (see docs/design/team-memory.md §10 risk #3 and docs/design/private-content.md).
-- So "private" here means private ORGANIZATION, not private content. True
-- content privacy is a dedicated coherent phase; half-hiding a doc while its
-- chunks remain searchable would be FALSE privacy, so we deliberately don't.
--
-- Backward compatible: existing scopes default to 'team'; single-user / dev /
-- background sessions (no app.member_id) retain full visibility.

BEGIN;

ALTER TABLE scopes
  ADD COLUMN visibility text NOT NULL DEFAULT 'team'
    CHECK (visibility IN ('team', 'private'));
ALTER TABLE scopes
  ADD COLUMN owner_member_id uuid REFERENCES tenant_members(member_id) ON DELETE SET NULL;

CREATE INDEX scopes_private_owner_idx
  ON scopes(tenant_id, owner_member_id)
  WHERE visibility = 'private';

-- ─── Visibility-aware RLS on scopes ─────────────────────────────────────────
-- Replace the plain tenant-isolation policy (from 009) with one that also
-- enforces private-scope visibility on reads. WITH CHECK stays tenant-only so
-- existing writes (surfacing-score updates by background jobs, etc.) are
-- unaffected; the app sets owner_member_id correctly at create time.
DROP POLICY scopes_tenant_isolation ON scopes;

CREATE POLICY scopes_tenant_isolation ON scopes
  USING (
    tenant_id = current_setting('app.tenant_id', true)::uuid
    AND (
      visibility = 'team'
      -- no member context (single-user / dev / background worker) ⇒ full access
      OR nullif(current_setting('app.member_id', true), '') IS NULL
      -- my own private scope
      OR owner_member_id = nullif(current_setting('app.member_id', true), '')::uuid
      -- admins / owners see everything in their team
      OR current_setting('app.member_role', true) IN ('admin', 'owner')
    )
  )
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- ─── Visibility-aware RLS on doc_scopes ─────────────────────────────────────
-- A doc_scopes row is visible only if the member may see the scope it links to.
-- Mirrors the scopes predicate explicitly (rather than relying on nested RLS)
-- so there are no recursion surprises. This keeps a private grouping — i.e.
-- "doc X is in Ada's private scope" — hidden from everyone but Ada/admins.
DROP POLICY doc_scopes_tenant_isolation ON doc_scopes;

CREATE POLICY doc_scopes_tenant_isolation ON doc_scopes
  USING (
    tenant_id = current_setting('app.tenant_id', true)::uuid
    AND (
      nullif(current_setting('app.member_id', true), '') IS NULL
      OR current_setting('app.member_role', true) IN ('admin', 'owner')
      OR EXISTS (
        SELECT 1 FROM scopes s
        WHERE s.scope_id = doc_scopes.scope_id
          AND (
            s.visibility = 'team'
            OR s.owner_member_id = nullif(current_setting('app.member_id', true), '')::uuid
          )
      )
    )
  )
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

INSERT INTO schema_migrations (version) VALUES ('035_private_scopes');

COMMIT;
