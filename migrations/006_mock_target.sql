-- migrations/006_mock_target.sql
-- Seeds a `Mock Co-Pilot (local stub)` row so `make run` is a frictionless
-- first-run experience without requiring a Synthea patient UUID. The
-- `MockCopilotClient` (defined in `target.py`) mimics the deployed Co-Pilot's
-- API shape and seeds deliberate vulnerabilities across the MVP categories
-- so the Judge has signal to score.
--
-- Two changes:
--   1. Widen the targets.target_type CHECK to include 'mock'.
--   2. Insert the seeded mock row.
--
-- Both idempotent.

DO $$ BEGIN
  ALTER TABLE targets DROP CONSTRAINT IF EXISTS targets_target_type_check;
  ALTER TABLE targets
    ADD CONSTRAINT targets_target_type_check
    CHECK (target_type IN ('copilot', 'generic_chat', 'openai_compat', 'mock'));
END $$;

INSERT INTO targets (name, target_type, target_url, config_json, notes)
VALUES (
  'Mock Co-Pilot (local stub)',
  'mock',
  'mock://local',
  '{}'::jsonb,
  'In-process stub. Use for first-run / offline demos — no env vars, no network.'
)
ON CONFLICT (name) DO NOTHING;
