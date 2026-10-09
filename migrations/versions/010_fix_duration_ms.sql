-- 010 — Fix pipeline_runs.duration_ms truncation bug.
--
-- Original definition cast `extract(epoch ...)` to int BEFORE multiplying by 1000,
-- which floors all sub-second runs to 0. Move the ::int cast to AFTER the *1000
-- so we preserve millisecond precision.
--
-- STORED generated columns can't be modified in place; drop + re-add.

ALTER TABLE pipeline_runs DROP COLUMN duration_ms;

ALTER TABLE pipeline_runs ADD COLUMN duration_ms int GENERATED ALWAYS AS (
  CASE WHEN finished_at IS NOT NULL
       THEN (extract(epoch from (finished_at - started_at)) * 1000)::int
       ELSE NULL
  END
) STORED;

INSERT INTO schema_migrations (version) VALUES ('010_fix_duration_ms');
