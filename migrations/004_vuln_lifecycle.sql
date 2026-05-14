-- 004 — vulnerability lifecycle (P1 from IMPLEMENTATION.md).
--
-- A FAIL'd attack_run is a raw signal; a vulnerability is the
-- triaged-into-record version with severity, state, and a vuln_report
-- (severity rationale, observed vs expected, repro, suggested fix).
-- Designed in `docs/components/database-schema.md` §2-§3.
--
-- State machine for `vulnerabilities.state`:
--   discovered → triaged → fix_proposed → fix_validated → closed
--              ↑                                            |
--              └─── reopened ─── (regression harness writes) ┘
--
-- For MVP, the Documentation Agent writes (discovered) on every FAIL.
-- Operator can transition discovered → triaged via the Vuln Board UI.
-- Fix_validated requires the regression harness (deferred to P2).

DO $$ BEGIN
  CREATE TYPE vuln_state AS ENUM (
    'discovered', 'triaged', 'fix_proposed', 'fix_validated', 'reopened', 'closed'
  );
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
  CREATE TYPE vuln_severity AS ENUM ('low', 'medium', 'high', 'critical');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS vulnerabilities (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  attack_run_id       UUID NOT NULL REFERENCES attack_runs(id),
  campaign_id         UUID NOT NULL REFERENCES campaigns(id),
  category            TEXT NOT NULL,
  subcategory         TEXT NOT NULL,
  severity            vuln_severity NOT NULL,
  state               vuln_state NOT NULL DEFAULT 'discovered',
  parent_vuln_id      UUID REFERENCES vulnerabilities(id),
  target_version      TEXT NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  triaged_at          TIMESTAMPTZ,
  triaged_by          TEXT,
  closed_at           TIMESTAMPTZ,
  closed_by           TEXT,
  CONSTRAINT vulnerabilities_attack_run_unique UNIQUE (attack_run_id)
);

CREATE INDEX IF NOT EXISTS vulnerabilities_state_idx ON vulnerabilities (state);
CREATE INDEX IF NOT EXISTS vulnerabilities_target_idx
  ON vulnerabilities (target_version, severity)
  WHERE state IN ('discovered', 'triaged', 'fix_proposed', 'reopened');
CREATE INDEX IF NOT EXISTS vulnerabilities_parent_idx
  ON vulnerabilities (parent_vuln_id)
  WHERE parent_vuln_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS vuln_reports (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  vuln_id              UUID NOT NULL REFERENCES vulnerabilities(id) ON DELETE CASCADE,
  severity_rationale   TEXT NOT NULL,
  observed_vs_expected TEXT NOT NULL,
  repro_steps          TEXT NOT NULL,
  suggested_fix        TEXT NOT NULL,
  defense_reference    TEXT,
  authored_by          TEXT NOT NULL DEFAULT 'documentation-agent-0',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS vuln_reports_vuln_idx ON vuln_reports (vuln_id);
