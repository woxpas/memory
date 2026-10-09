-- 015 — Document sensitivity classification + tenant policy switch.
--
-- Three independent tiers populate sensitivity_class (OR-combined):
--   1. Manual flag at upload     (highest priority; user knows best)
--   2. Summarize-LLM judgment    (Tier 2; produced by summarizer prompt)
--   3. Heuristics + summary topics (Tier 3; post-summarize refine stage)
--
-- A fast subset of Tier 3 (hard heuristics: IBAN/BIC/CC-Luhn/ICD-10/SSN)
-- runs at detect time so the summarize cloud-policy gate has something to
-- block on before any cloud LLM call.
--
-- The cloud-policy gate uses sensitivity_class as a STRICTER, INDEPENDENT
-- switch from allow_cloud_for_personal_data. A user opting into cloud for
-- personal data does NOT auto-unlock cloud for sensitive data.

-- ─── documents.sensitivity_class ───────────────────────────────────────────
ALTER TABLE documents
  ADD COLUMN sensitivity_class text NOT NULL DEFAULT 'standard'
    CHECK (sensitivity_class IN ('standard', 'sensitive'));

-- Free-form audit trail: who/what flagged this and why. Examples:
--   "manual:user"
--   "heuristic:iban_match"
--   "llm:contains medical records"
--   "topic:medical_report"
-- Multiple tiers can chain ("heuristic:iban_match;llm:contains bank info").
ALTER TABLE documents
  ADD COLUMN sensitivity_reason text;

CREATE INDEX documents_sensitivity_idx
  ON documents(tenant_id, sensitivity_class)
  WHERE sensitivity_class = 'sensitive';

-- ─── tenants.allow_cloud_for_sensitive_data ────────────────────────────────
-- Independent of allow_cloud_for_personal_data (added in 014). Stricter
-- default-off switch. Flipping requires explicit user action via CLI.
ALTER TABLE tenants
  ADD COLUMN allow_cloud_for_sensitive_data boolean NOT NULL DEFAULT false;

INSERT INTO schema_migrations (version) VALUES ('015_sensitivity');
