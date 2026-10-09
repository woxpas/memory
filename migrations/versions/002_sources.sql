-- 002 — Sources + encrypted credentials
--
-- Every capture surface (file upload, voice note, MCP server, browser ext, slack, agent, calendar)
-- is registered as a source. Source credentials (OAuth tokens, API keys) are encrypted
-- under a field key derived from the tenant DEK.

CREATE TABLE sources (
  source_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  type             text NOT NULL
                    CHECK (type IN (
                      'file_upload',
                      'voice_note',
                      'mcp_inbound',
                      'mcp_outbound',
                      'browser_extension',
                      'slack',
                      'email',
                      'agent',
                      'calendar',
                      'api_custom'
                    )),
  name             text NOT NULL,
  description      text,

  config           jsonb NOT NULL DEFAULT '{}'::jsonb,
  status           text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','paused','error','revoked')),
  error_state      jsonb,

  last_synced_at   timestamptz,
  last_seen_at     timestamptz,

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT sources_name_unique UNIQUE (tenant_id, type, name)
);
CREATE INDEX sources_tenant_type_idx ON sources(tenant_id, type) WHERE status = 'active';

CREATE TABLE source_credentials (
  source_id        uuid PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,
  tenant_id        uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

  encrypted_blob   bytea NOT NULL,        -- AES-256-GCM under field key derived from DEK
  iv               bytea NOT NULL,
  tag              bytea NOT NULL,
  field_key_version int  NOT NULL DEFAULT 1,

  expires_at       timestamptz,
  rotated_at       timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('002_sources');
