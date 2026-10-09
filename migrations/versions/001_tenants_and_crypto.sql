-- 001 — Tenants + crypto envelope
--
-- The tenants table is the root of the security model. Each install has at least one tenant.
-- Passphrase → Argon2id KDF → KEK → unwraps DEK (envelope encryption).
-- DEK never persists in plaintext; only the encrypted_dek + KDF params live here.
-- App-level field keys are derived from DEK at session unlock and held in mlock'd memory.

CREATE TABLE schema_migrations (
  version       text PRIMARY KEY,
  applied_at    timestamptz NOT NULL DEFAULT now(),
  checksum      bytea
);

CREATE TABLE tenants (
  tenant_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name           text NOT NULL,

  -- passphrase auth
  passphrase_verifier    bytea NOT NULL,                 -- Argon2id hash for verification
  kdf_params             jsonb NOT NULL,                 -- {algo, time_cost, memory_kib, parallelism, salt_b64, version}

  -- envelope encryption
  encrypted_dek          bytea NOT NULL,                 -- DEK wrapped under KEK derived from passphrase
  encrypted_dek_iv       bytea NOT NULL,
  encrypted_dek_tag      bytea NOT NULL,
  dek_version            int  NOT NULL DEFAULT 1,        -- bumps on rotation

  -- recovery (optional)
  recovery_verifier      bytea,
  recovery_kdf_params    jsonb,
  encrypted_dek_recovery bytea,
  encrypted_dek_recovery_iv  bytea,
  encrypted_dek_recovery_tag bytea,

  -- crypto policy
  crypto_policy          jsonb NOT NULL DEFAULT
    '{"sensitive_fields": ["text","summary","raw_uri"], "rotate_after_days": 365}'::jsonb,

  -- tenancy state
  status                 text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','locked','disabled')),
  failed_unlock_count    int  NOT NULL DEFAULT 0,
  locked_until           timestamptz,

  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tenants_status_idx ON tenants(status) WHERE status <> 'active';

-- Sessions are not persisted; tracked here only for revocation/rotation accounting.
CREATE TABLE tenant_session_log (
  session_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  established_at         timestamptz NOT NULL DEFAULT now(),
  ended_at               timestamptz,
  reason                 text,                            -- idle | shutdown | rotation | manual
  client_kind            text,                            -- boot_service | api | worker
  client_fingerprint     text                             -- non-pii: process id, container id
);
CREATE INDEX tenant_session_log_tenant_idx ON tenant_session_log(tenant_id, established_at DESC);

INSERT INTO schema_migrations (version) VALUES ('001_tenants_and_crypto');
