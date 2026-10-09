-- 028 — Phase 5a.3.1: extend resurface_artifact_kind for predicted/prepared actions.
--
-- Two new artifact kinds split the "what should happen next" surface:
--
--   predicted_action  — memory-internal opportunity detected by the dreaming
--                       worker over the user's CAPTURED knowledge graph.
--                       Memory generates these autonomously; idempotent
--                       per (action_kind, subject) so reruns refresh
--                       rather than duplicate.
--
--   prepared_action   — composed action posted BACK to memory by an
--                       agent (any MCP agent) after the agent
--                       traversed an external service via its own MCP and
--                       used memory context to draft something. Memory is
--                       the shared persistence + approval state across
--                       agents.
--
-- ALTER TYPE ADD VALUE cannot run inside a transaction block — must be
-- standalone. The accompanying state-machine columns + indexes ship in
-- migration 029, which depends on these enum values being visible.

ALTER TYPE resurface_artifact_kind ADD VALUE IF NOT EXISTS 'predicted_action';
ALTER TYPE resurface_artifact_kind ADD VALUE IF NOT EXISTS 'prepared_action';

INSERT INTO schema_migrations (version) VALUES ('028_predicted_prepared_actions');
