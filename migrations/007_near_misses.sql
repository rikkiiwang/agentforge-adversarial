-- migrations/007_near_misses.sql
-- ARCHITECTURE.md §6: per-PARTIAL lifecycle tracking.
--
-- The judge emits PARTIAL when a target response is ambiguous (hedged
-- refusal, partial leak, off-topic deflection that's not a clean refuse).
-- Today PARTIAL runs only color the heat-map; this table promotes them to
-- first-class lifecycle objects so the platform can:
--   - show "active near-misses being explored" on the dashboard
--   - track which variant lineage came from each PARTIAL
--   - record the terminal state (escalated / exhausted / budget_capped) when
--     the mutator's re-entry rounds complete
--
-- State transitions (managed by the orchestrator + graph nodes; this
-- migration only ships the schema):
--   exploring   → initial state on PARTIAL detection
--   escalated   → at least one variant from partial-reentry came back FAIL
--   exhausted   → all variants came back PASS (defense held against the
--                 boundary)
--   budget_capped → max_rounds hit before any escalation/exhaustion
--
-- Idempotent: re-running this migration on a populated DB is a no-op.

CREATE TABLE IF NOT EXISTS near_misses (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attack_run_id    UUID NOT NULL UNIQUE REFERENCES attack_runs(id) ON DELETE CASCADE,
    campaign_id      UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    case_id          TEXT NOT NULL,
    category         TEXT NOT NULL,
    subcategory      TEXT NOT NULL,
    severity         TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'exploring'
                     CHECK (state IN ('exploring', 'escalated', 'exhausted', 'budget_capped')),
    variant_count    INT  NOT NULL DEFAULT 0,
    escalating_run_id UUID REFERENCES attack_runs(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS near_misses_campaign_state_idx
    ON near_misses(campaign_id, state);

CREATE INDEX IF NOT EXISTS near_misses_state_updated_idx
    ON near_misses(state, updated_at DESC);
