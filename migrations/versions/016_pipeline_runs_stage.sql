-- 016 — Extend pipeline_runs.stage CHECK to cover Phase 2 stages.
--
-- The original constraint listed only the v0.1 ingestion stages. Phase 2
-- adds: sensitivity_refine, extract_facts (LLM extraction), link (entity
-- disambiguation + predicate normalization), conflict_detect (Type-2/Type-3
-- conflict surfacing). Pre-add them all so we don't need a constraint bump
-- per stage.

ALTER TABLE pipeline_runs DROP CONSTRAINT IF EXISTS pipeline_runs_stage_check;

ALTER TABLE pipeline_runs
  ADD CONSTRAINT pipeline_runs_stage_check CHECK (stage IN (
    -- v0.1 ingestion stages
    'ingress', 'av_scan', 'type_detect', 'extract', 'normalize',
    'summarize', 'chunk', 'embed', 'index', 'classify', 'dispatch',
    'supersession_check',
    -- Phase 2.0 — sensitivity classification refinement
    'sensitivity_refine',
    -- Phase 2.x — extraction / linking / conflict pipeline
    'extract_facts', 'link', 'conflict_detect'
  ));

INSERT INTO schema_migrations (version) VALUES ('016_pipeline_runs_stage');
