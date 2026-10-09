-- Woxpas v0.1 — extensions required across the system.
-- This file runs on first container init (docker-entrypoint-initdb.d).

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gist";
CREATE EXTENSION IF NOT EXISTS "vector";

-- App role for runtime queries (least-privilege; cannot create or alter).
-- The migration role (POSTGRES_USER) owns DDL; the app role owns DML only.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'woxpas_app') THEN
    CREATE ROLE woxpas_app NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'woxpas_audit') THEN
    -- append-only role for audit_log
    CREATE ROLE woxpas_audit NOLOGIN NOINHERIT;
  END IF;
END$$;
