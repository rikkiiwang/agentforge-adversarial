-- 002 — targets table. Lets the platform attack multiple AI systems
-- (different Co-Pilot deployments, OpenAI-compatible chat endpoints,
-- generic HTTP/JSON LLM services) instead of one hardcoded URL.

CREATE TABLE IF NOT EXISTS targets (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          TEXT NOT NULL UNIQUE,
  target_type   TEXT NOT NULL CHECK (target_type IN ('copilot', 'generic_chat', 'openai_compat')),
  target_url    TEXT NOT NULL,
  config_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed: the deployed OpenEMR Clinical Co-Pilot. config_json carries the
-- type-specific fields (patient_id, physician_user_id for the copilot type).
INSERT INTO targets (name, target_type, target_url, config_json, notes)
VALUES (
  'OpenEMR Clinical Co-Pilot (deployed)',
  'copilot',
  'https://copilot-production-b532.up.railway.app',
  '{"patient_id": "", "physician_user_id": "admin"}'::jsonb,
  'Default target. Set patient_id via the dashboard Add-target form or by editing config_json.'
)
ON CONFLICT (name) DO NOTHING;

-- Add target_id to campaigns so each campaign records which system it attacked.
ALTER TABLE campaigns
  ADD COLUMN IF NOT EXISTS target_id UUID REFERENCES targets(id);
