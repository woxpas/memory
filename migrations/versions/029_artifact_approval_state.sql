-- 029 — Phase 5a.3.1: approval state machine on resurface_artifacts.
--
-- Depends on migration 028 (enum values predicted_action + prepared_action
-- must be committed before this migration's partial unique index can be
-- created — its WHERE clause references the literal 'predicted_action').
--
-- Approval state machine (prepared_action only — predicted_action is
-- read-only suggestions):
--
--     drafted ──approve──▶ approved ──mark-executed──▶ executed
--        │
--        ├──reject──▶ rejected
--        │
--        └──TTL expires──▶ expired
--
-- predicted_action artifacts default to status='drafted' meaning "memory
-- has surfaced this; agent should consider it". Agents may compose a
-- downstream prepared_action and transition that through the lifecycle;
-- the predicted_action itself stays drafted until TTL or until the
-- underlying condition resolves on the next dreaming pass — at which
-- point either the row gets UPSERTed with refreshed content, or the
-- reaper expires it.

BEGIN;

-- A. Approval-status enum.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'resurface_artifact_status') THEN
    CREATE TYPE resurface_artifact_status AS ENUM (
      'drafted',
      'approved',
      'executed',
      'rejected',
      'expired'
    );
  END IF;
END$$;

-- B. State + metadata columns. All nullable except status (default drafted)
-- and requires_approval (default true — safer-by-default; agents posting
-- safe read-only actions explicitly set false).
ALTER TABLE resurface_artifacts
  ADD COLUMN IF NOT EXISTS status            resurface_artifact_status
                                              NOT NULL DEFAULT 'drafted',
  ADD COLUMN IF NOT EXISTS approved_at       timestamptz  NULL,
  ADD COLUMN IF NOT EXISTS executed_at       timestamptz  NULL,
  ADD COLUMN IF NOT EXISTS rejected_at       timestamptz  NULL,
  ADD COLUMN IF NOT EXISTS rejected_reason   text         NULL,
  ADD COLUMN IF NOT EXISTS requires_approval boolean      NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS agent_id          text         NULL,
  ADD COLUMN IF NOT EXISTS subject_signature text         NULL;

COMMENT ON COLUMN resurface_artifacts.status IS
  'Phase 5a.3.1 — Approval state machine. predicted_action stays drafted '
  'until TTL/condition resolution; prepared_action transitions through '
  'drafted -> approved -> executed (or -> rejected).';
COMMENT ON COLUMN resurface_artifacts.requires_approval IS
  'Phase 5a.3.1 — Hint to the agent: true means destructive/sensitive '
  'and the agent UI should explicitly confirm before transitioning to '
  'executed. Memory does NOT enforce; it is metadata only.';
COMMENT ON COLUMN resurface_artifacts.agent_id IS
  'Phase 5a.3.1 — Which agent posted this prepared_action. Null for '
  'memory-internal predicted_action artifacts. Lets list endpoints '
  'filter by author when multiple agents are wired up.';
COMMENT ON COLUMN resurface_artifacts.subject_signature IS
  'Phase 5a.3.1 — Idempotency key for predicted_action. '
  'Format <action_kind>:<subject_kind>:<subject_id>. UPSERT keyed on '
  '(tenant_id, subject_signature) where kind=predicted_action ensures '
  'dreaming reruns refresh rather than duplicate. Null on other kinds.';

-- C. Indexes.

-- Status-aware list queries (e.g. "list pending prepared_actions for
-- agent X expiring in 24h"). Partial index keeps it small — historic
-- expired/executed/rejected rows aren't in the hot path.
CREATE INDEX IF NOT EXISTS ix_resurface_artifacts_active_status
  ON resurface_artifacts (tenant_id, kind, status, expires_at)
  WHERE status IN ('drafted', 'approved');

-- Agent-keyed lookup for "what has this agent posted recently".
CREATE INDEX IF NOT EXISTS ix_resurface_artifacts_agent
  ON resurface_artifacts (tenant_id, agent_id, generated_at DESC)
  WHERE agent_id IS NOT NULL;

-- Idempotency for predicted_action — UPSERT keyed on (tenant_id, signature).
-- Filter is on kind=predicted_action so prepared_action drafts are not
-- constrained (multiple iterations on the same subject allowed).
CREATE UNIQUE INDEX IF NOT EXISTS ux_predicted_action_signature
  ON resurface_artifacts (tenant_id, subject_signature)
  WHERE kind = 'predicted_action' AND subject_signature IS NOT NULL;

-- D. RLS — already applied to resurface_artifacts in 022. New columns
-- inherit the row-level policy automatically.

INSERT INTO schema_migrations (version) VALUES ('029_artifact_approval_state');

COMMIT;
