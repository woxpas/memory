-- 037 — Team memory, Phase A5: member re-key status (DEK-rotation rails)
--
-- Forward secrecy against a removed member requires rotating the tenant DEK and
-- re-encrypting data (see docs/design/dek-rotation.md). With the current
-- symmetric passphrase→KEK model, a rotation can only re-wrap the new DEK for
-- members whose passphrase is present at rotation time (realistically just the
-- admin triggering it). Every OTHER surviving member must re-key — they land in
-- 'needs_rekey' and are re-admitted via `woxpas-cli add-member` (which wraps the
-- new DEK under a fresh passphrase for them).
--
-- This migration just adds the status value. The unlock path already refuses
-- any member whose status isn't 'active' (so 'needs_rekey' can't unlock), and
-- a dek_version guard (code) rejects a stale envelope even if still 'active'.
-- Numbered 037 to leave 036 for the in-flight processing_profile-default fix.

BEGIN;

ALTER TABLE tenant_members DROP CONSTRAINT tenant_members_status_check;
ALTER TABLE tenant_members ADD CONSTRAINT tenant_members_status_check
  CHECK (status IN ('active', 'suspended', 'removed', 'needs_rekey'));

INSERT INTO schema_migrations (version) VALUES ('037_member_rekey_status');

COMMIT;
