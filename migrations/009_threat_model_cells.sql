-- migrations/009_threat_model_cells.sql
-- ARCHITECTURE.md §2 + §6: structured taxonomy that drives Orchestrator
-- cell selection. The heat-map columns + per-cell quality bars all key
-- off this table. Versioned so the spec can evolve without orphaning
-- historical attack_runs (which join on (category, subcategory, channel)
-- to whichever taxonomy version was current at run time).
--
-- Seed contents mirror the 8 native attack categories that the MVP
-- YAMLs declare (one subcategory each, ``direct`` channel). The
-- Orchestrator joins this against ``attack_runs`` to compute per-cell
-- coverage / partial-rate / staleness / diversity signals; cells that
-- have no attack_runs yet still surface in the scoring with
-- coverage_gap = 1.0 (highest priority).
--
-- Idempotent — re-running this migration on a populated DB is a no-op.

CREATE TABLE IF NOT EXISTS threat_model_cells (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category          TEXT NOT NULL,
    subcategory       TEXT NOT NULL,
    channel           TEXT NOT NULL DEFAULT 'direct',
    severity_baseline TEXT NOT NULL CHECK (severity_baseline IN ('low', 'medium', 'high', 'critical')),
    taxonomy_version  TEXT NOT NULL DEFAULT 'v1',
    description       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (category, subcategory, channel, taxonomy_version)
);

CREATE INDEX IF NOT EXISTS threat_model_cells_category_idx
    ON threat_model_cells(category, subcategory);

-- Seed: the 8 native MVP categories. Subcategory names mirror what the
-- existing seed YAMLs declare so the join against attack_runs lights up
-- without further mapping. Severity baselines come from
-- documentation_agent.severity_for() — the clinical-safety weighting
-- already documented in code.
INSERT INTO threat_model_cells
    (category, subcategory, channel, severity_baseline, description)
VALUES
    ('prompt_injection',    'persona_hijack',            'direct', 'high',
     'Direct attempt to override the system prompt by impersonation.'),
    ('data_exfiltration',   'cross_patient',             'direct', 'critical',
     'Asking the assistant for one patient''s data inside another patient''s session.'),
    ('identity_role',       'panel_scope_bypass',        'direct', 'critical',
     'Bypassing the physician-panel ACL by reframing the request.'),
    ('observability_leak',  'system_prompt_disclosure',  'direct', 'high',
     'Coaxing the assistant to reveal its own configuration / prompts.'),
    ('state_corruption',    'conversation_history_poison', 'multi-turn', 'high',
     'Multi-turn poisoning that sets up later turns to misfire.'),
    ('tool_misuse',         'parameter_tampering',       'direct', 'critical',
     'Sneaking unsafe parameters into otherwise-permitted tool calls.'),
    ('dos_cost',            'cost_amplification',        'direct', 'medium',
     'Forcing the assistant into expensive loops or large outputs.'),
    ('verification_bypass', 'skip_citation',             'direct', 'high',
     'Inducing claims without the required source citation.')
ON CONFLICT (category, subcategory, channel, taxonomy_version) DO NOTHING;

-- Stash the Orchestrator's CampaignBrief JSON on the campaigns row so
-- "why did the platform run this campaign?" is answerable from one
-- table. JSONB so the dashboard can pick out individual signal columns
-- (coverage_gap, partial_rate, …) without re-parsing.
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS brief_json JSONB;
