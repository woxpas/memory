-- 032 — Team memory, Phase A0: tenant_members
--
-- Re-interprets a tenant as a *team*: a tenant still owns exactly one DEK, but
-- now many human members can each unlock it. Each member stores their OWN copy
-- of the shared tenant DEK, wrapped under a KEK derived from THEIR passphrase
-- (same envelope primitive as tenants, applied N times — see
-- woxpas_storage/envelope.py:wrap_dek_for_passphrase). No plaintext DEK is ever
-- persisted; adding a member re-wraps an already-unwrapped DEK in memory.
--
-- Backward-compatible: every existing single-person tenant is backfilled to a
-- team of one (an 'owner' member reusing the tenant's existing envelope), so
-- dev / single_tenant_local flows are unchanged until a second member is added.
--
-- Security (Pillar 3): RLS-isolated per tenant like every other table; the
-- per-member envelope columns mirror tenants exactly; member removal is a
-- separate concern (revoke here, DEK rotation is a later sub-phase).

BEGIN;

CREATE TABLE tenant_members (
  member_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  -- who
  display_name         text NOT NULL,
  external_subject     text,                      -- JWT 'sub' / IdP id; NULL until wired to an IdP
  self_entity_id       uuid REFERENCES entities(entity_id) ON DELETE SET NULL,

  -- role / lifecycle
  role                 text NOT NULL DEFAULT 'member'
                         CHECK (role IN ('owner','admin','member','viewer')),
  status               text NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active','suspended','removed')),

  -- per-member envelope: the SAME tenant DEK, wrapped under THIS member's KEK
  passphrase_verifier  bytea NOT NULL,            -- Argon2id PHC verifier
  kdf_params           jsonb NOT NULL,            -- {algo,time_cost,memory_kib,parallelism,salt_b64,version}
  encrypted_dek        bytea NOT NULL,            -- tenant DEK wrapped under member KEK
  encrypted_dek_iv     bytea NOT NULL,
  encrypted_dek_tag    bytea NOT NULL,
  dek_version          int  NOT NULL DEFAULT 1,   -- must track tenants.dek_version

  invited_by           uuid REFERENCES tenant_members(member_id) ON DELETE SET NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  -- a given IdP subject maps to at most one member per team (NULLs are distinct,
  -- so multiple not-yet-wired members are allowed)
  UNIQUE (tenant_id, external_subject)
);

CREATE INDEX tenant_members_tenant_idx ON tenant_members(tenant_id);
CREATE INDEX tenant_members_active_idx ON tenant_members(tenant_id, status) WHERE status = 'active';
CREATE INDEX tenant_members_self_entity_idx
  ON tenant_members(self_entity_id) WHERE self_entity_id IS NOT NULL;

-- Standard tenant isolation (reuses the helper from 009).
SELECT woxpas_apply_tenant_rls('tenant_members'::regclass);

-- ─── Backfill: every existing tenant becomes a team of one ──────────────────
-- Copies the tenant's existing passphrase envelope into an 'owner' member, so
-- the current passphrase keeps working with no re-derivation. Runs as the
-- migration superuser (RLS bypassed), hence the set-based form across tenants.
INSERT INTO tenant_members (
  tenant_id, display_name, self_entity_id, role, status,
  passphrase_verifier, kdf_params,
  encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version
)
SELECT
  t.tenant_id, t.display_name, t.self_entity_id, 'owner', 'active',
  t.passphrase_verifier, t.kdf_params,
  t.encrypted_dek, t.encrypted_dek_iv, t.encrypted_dek_tag, t.dek_version
FROM tenants t
WHERE NOT EXISTS (
  SELECT 1 FROM tenant_members m
  WHERE m.tenant_id = t.tenant_id AND m.role = 'owner'
);

INSERT INTO schema_migrations (version) VALUES ('032_tenant_members');

COMMIT;
