-- 011 — Add 'supersession_check' to pipeline_runs.stage CHECK constraint.
--
-- The supersession candidate stage runs after summarize and writes a
-- pipeline_runs row. Without this addition the existing CHECK rejects it.

ALTER TABLE pipeline_runs DROP CONSTRAINT IF EXISTS pipeline_runs_stage_check;

ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_stage_check CHECK (
  stage IN (
    'ingress','av_scan','type_detect','extract','normalize',
    'summarize','chunk','embed','index','classify','dispatch',
    'supersession_check'
  )
);

INSERT INTO schema_migrations (version) VALUES ('011_supersession_stage');
