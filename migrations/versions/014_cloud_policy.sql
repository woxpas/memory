-- 014 — Per-tenant cloud-data policy.
--
-- Default false → personal-data profiles {personal, voice_note in personal,
-- identity_seed} never get sent to cloud LLMs without explicit opt-in.
-- The LLM backend gate reads this column before any egress call; a hard fail
-- (not silent skip) when policy is false and the doc is in a gated profile.

ALTER TABLE tenants
  ADD COLUMN allow_cloud_for_personal_data boolean NOT NULL DEFAULT false;

INSERT INTO schema_migrations (version) VALUES ('014_cloud_policy');
