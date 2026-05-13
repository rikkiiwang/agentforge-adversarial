-- MVP migration: strict subset of docs/components/database-schema.md.
-- Covers campaigns, attack_queue, attack_runs only. Same column names and
-- constraints as the full schema so follow-up migrations are additive.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- §7 campaigns (declared before attack_queue per migration order).
CREATE TABLE IF NOT EXISTS campaigns (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  target_version  TEXT NOT NULL,
  notes           TEXT
);

-- §8 attack_queue
DO $$ BEGIN
  CREATE TYPE attack_source AS ENUM ('direct', 'random', 'mutator', 'regression', 'class_probe');
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  CREATE TYPE queue_state AS ENUM ('queued', 'dispatching', 'dispatched', 'failed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS attack_queue (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id            UUID NOT NULL REFERENCES campaigns(id),
  source                 attack_source NOT NULL,
  category               TEXT NOT NULL,
  subcategory            TEXT NOT NULL,
  channel                TEXT NOT NULL,
  attack_prompt          TEXT NOT NULL,
  red_team_subagent_id   TEXT NOT NULL,
  red_team_model         TEXT NOT NULL,
  expected_failure_mode  TEXT NOT NULL,
  priority_score         REAL NOT NULL DEFAULT 0.5,
  state                  queue_state NOT NULL DEFAULT 'queued',
  enqueued_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  dispatched_at          TIMESTAMPTZ,
  attack_run_id          UUID,
  failure_reason         TEXT
);

CREATE INDEX IF NOT EXISTS attack_queue_state_queued_idx
  ON attack_queue (priority_score DESC, enqueued_at)
  WHERE state = 'queued';

-- §1 attack_runs
DO $$ BEGIN
  CREATE TYPE judge_verdict AS ENUM ('pass', 'partial', 'fail');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS attack_runs (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  queue_entry_id         UUID UNIQUE NOT NULL,
  campaign_id            UUID NOT NULL REFERENCES campaigns(id),
  case_id                TEXT NOT NULL,
  source                 attack_source NOT NULL,
  category               TEXT NOT NULL,
  subcategory            TEXT NOT NULL,
  channel                TEXT NOT NULL,
  red_team_subagent_id   TEXT NOT NULL,
  red_team_model         TEXT NOT NULL,
  attack_prompt          TEXT NOT NULL,
  expected_failure_mode  TEXT NOT NULL,
  observed_output        TEXT NOT NULL,
  target_version         TEXT NOT NULL,
  cost_usd               NUMERIC(10,5) NOT NULL DEFAULT 0,
  latency_ms             INT NOT NULL,
  dispatcher_version     TEXT NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Judge cols (UPDATEd in a separate flow; all-or-nothing per CHECK).
  judge_verdict          judge_verdict,
  judge_reasoning        TEXT,
  judge_rubric_version   TEXT,
  category_validated     BOOLEAN,
  judged_at              TIMESTAMPTZ,
  CONSTRAINT attack_runs_judge_atomic CHECK (
    (judge_verdict IS NULL AND judge_reasoning IS NULL AND judge_rubric_version IS NULL
       AND category_validated IS NULL AND judged_at IS NULL)
    OR
    (judge_verdict IS NOT NULL AND judge_reasoning IS NOT NULL AND judge_rubric_version IS NOT NULL
       AND category_validated IS NOT NULL AND judged_at IS NOT NULL)
  )
);

-- DEFERRABLE FK back to attack_queue (per database-schema.md §8).
DO $$ BEGIN
  ALTER TABLE attack_runs
    ADD CONSTRAINT attack_runs_queue_entry_fk
    FOREIGN KEY (queue_entry_id) REFERENCES attack_queue(id)
    DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN null; END $$;
