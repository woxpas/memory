-- 027 — Phase 5a.4.1: track skill_id on agent observations.
--
-- Skills (Claude Skills, NOYU bundled workflows, GPTs, Cursor instructions,
-- etc.) are higher-level capabilities that bundle multiple tools with a
-- procedural recipe. They're a different signal than raw tool_called:
--
--   tool_called='memory.ask',  skill_id=null            ← direct tool call
--   tool_called='memory.ask',  skill_id='writing'       ← within a skill
--   tool_called='writing',     skill_id=null            ← skill exposed as tool
--
-- Tracking both dimensions lets PatternDetector derive richer patterns
-- (tool + skill combinations) once data accumulates. Nullable column —
-- agents that don't expose skills (or 3rd-party MCP integrations that
-- don't surface them yet) leave it null and everything still works.
--
-- The index supports both:
--   (a) "what skills has this tenant used?"  (skill frequency aggregation)
--   (b) "for skill X, what tool patterns exist?"  (skill-keyed patterns)

BEGIN;

ALTER TABLE resurface_observations
  ADD COLUMN skill_id text NULL;
COMMENT ON COLUMN resurface_observations.skill_id IS
  'Phase 5a.4.1 — optional skill identifier when the tool was invoked '
  'as part of a higher-level skill. Null for direct tool calls or for '
  'agents that do not expose skill information.';

CREATE INDEX ix_resurface_obs_skill
  ON resurface_observations (tenant_id, skill_id, observed_at DESC)
  WHERE skill_id IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('027_observations_skill_id');

COMMIT;
