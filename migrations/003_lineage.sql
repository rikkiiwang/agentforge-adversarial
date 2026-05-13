-- 003 — class-probe lineage for the LangGraph FAIL fan-out.
--
-- When the Judge marks an attack FAIL, the orchestrator (now a LangGraph
-- conditional edge) fans out to a Class-Probe subagent that generates 10
-- boundary variants of the failing attack. Each variant is recorded with
-- parent_id = the original FAIL's id, and round_num one greater than the
-- parent's. max_rounds bounds the loop so a single FAIL can't cascade
-- indefinitely.

DO $$ BEGIN
  ALTER TYPE attack_source ADD VALUE IF NOT EXISTS 'class_probe';
EXCEPTION WHEN duplicate_object THEN null; END $$;

ALTER TABLE attack_runs
  ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES attack_runs(id),
  ADD COLUMN IF NOT EXISTS round_num INTEGER NOT NULL DEFAULT 0;

ALTER TABLE attack_queue
  ADD COLUMN IF NOT EXISTS parent_id UUID,
  ADD COLUMN IF NOT EXISTS round_num INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS attack_runs_parent_idx ON attack_runs (parent_id)
  WHERE parent_id IS NOT NULL;
