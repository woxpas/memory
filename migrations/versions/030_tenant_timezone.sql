-- 030 — Phase 5a.3.2 fix B: tenants.timezone for local-time aware
-- pattern detection.
--
-- ResurfaceEngine._fetch_tenant_timezone (Phase 5a.1) already reads this
-- column with a graceful UTC fallback when missing. PatternDetector
-- (Phase 5a.4 + 5a.3.1) needs it to bucket observation hours into the
-- USER'S local time so 'most_active_hour=9' means "9am where the user
-- is" rather than "9am UTC". Without this, time-bucketed prefs are
-- worthless for any user outside UTC+0.
--
-- IANA timezone names (e.g. 'Europe/Lisbon'). NULL is permitted (NULL or
-- 'UTC' both fall back to UTC at read time). Default 'UTC' so existing
-- code paths get a sensible value without per-tenant migration writes.

BEGIN;

ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS timezone text NULL DEFAULT 'UTC';

COMMENT ON COLUMN tenants.timezone IS
  'IANA timezone name for the tenant (e.g. ''Europe/Lisbon''). Used by '
  'resurfacing layers to bucket observation hours into local time. NULL '
  'or unrecognized → UTC fallback at read time.';

INSERT INTO schema_migrations (version) VALUES ('030_tenant_timezone');

COMMIT;
