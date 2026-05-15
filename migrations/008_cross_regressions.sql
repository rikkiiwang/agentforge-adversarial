-- migrations/008_cross_regressions.sql
-- ARCHITECTURE.md §6: cross-version regression detection.
--
-- Records the event "a case that previously PASSED on an older
-- target_version now FAILs on the current one." This is structurally
-- different from same-version regression (which the regression_schedule
-- and slim CLI harness already cover): it surfaces cases where a *fix on
-- another part of the target* — typically a deploy that changes behavior
-- in an adjacent code path — broke something that used to be safe.
--
-- Written by ``analysis.cross_regressions.detect_for_campaign`` after
-- each campaign completes. One row per (case_id, target_version
-- transition); detection re-runs are idempotent on the (attack_run_id,
-- prior_attack_run_id) composite.
--
-- Acknowledgement is human-driven via the dashboard. Operators set
-- ``acknowledged_at`` (and optionally ``acknowledged_by``) so the
-- "unacked cross-regressions" board doesn't keep firing on the same
-- evidence.
--
-- Idempotent: re-running this migration on a populated DB is a no-op.

CREATE TABLE IF NOT EXISTS cross_regressions (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attack_run_id          UUID NOT NULL REFERENCES attack_runs(id) ON DELETE CASCADE,
    prior_attack_run_id    UUID REFERENCES attack_runs(id) ON DELETE SET NULL,
    case_id                TEXT NOT NULL,
    category               TEXT NOT NULL,
    current_target_version TEXT NOT NULL,
    prior_target_version   TEXT NOT NULL,
    detected_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at        TIMESTAMPTZ,
    acknowledged_by        TEXT,
    UNIQUE (attack_run_id, prior_attack_run_id)
);

CREATE INDEX IF NOT EXISTS cross_regressions_unack_idx
    ON cross_regressions(detected_at DESC)
 WHERE acknowledged_at IS NULL;

CREATE INDEX IF NOT EXISTS cross_regressions_case_id_idx
    ON cross_regressions(case_id, detected_at DESC);
