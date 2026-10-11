-- 032 — Fix stale documents.processing_profile column default.
--
-- Migration 004 declared the column with `DEFAULT 'mixed_note'`. Migration
-- 012 consolidated the taxonomy and replaced documents_processing_profile_check
-- with a new value set that does NOT include 'mixed_note' (that old value was
-- migrated to 'personal'). The column DEFAULT was never updated, so it still
-- points at a value the CHECK constraint now rejects.
--
-- Effect: any INSERT into documents that omits processing_profile and relies
-- on the column default raises CheckViolationError. This is latent today only
-- because the ingestion pipeline's detect stage always sets the profile
-- explicitly — but the default is a footgun for any future insert path.
--
-- Fix: point the default at 'personal', which is the exact value 012 chose as
-- the successor to the old 'mixed_note' neutral default (012 lines 23-24). We
-- do NOT edit historical migrations.

BEGIN;

ALTER TABLE documents
  ALTER COLUMN processing_profile SET DEFAULT 'personal';

INSERT INTO schema_migrations (version) VALUES ('038_fix_processing_profile_default');

COMMIT;
