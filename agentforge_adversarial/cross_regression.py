"""Cross-version regression detection.

ARCHITECTURE.md §6: when a case that previously PASSED on an older
``target_version`` now FAILs on the current one, that is a *cross-
regression* — typically a fix elsewhere in the target broke something
adjacent that was safe before. Distinct from same-version regression
(which the regression_schedule harness covers): the signal is the
version-transition, not the case-was-already-vulnerable history.

Detection runs once per campaign, after every attack run has been
judged. The query joins attack_runs against itself: for every FAIL in
the just-finished campaign, find the most recent prior judged run of
the same case_id on a *different* target_version that had verdict
``pass``. Each match is one cross-regression row. Re-runs of detection
are idempotent on the ``(attack_run_id, prior_attack_run_id)`` unique
constraint.
"""
from __future__ import annotations

from uuid import UUID

import asyncpg


async def detect_for_campaign(
    conn: asyncpg.Connection,
    campaign_id: UUID,
) -> int:
    """Insert cross_regression rows for every FAIL in the campaign whose
    case_id last PASSed on a different target_version.

    Returns the number of rows inserted. Safe to call multiple times for
    the same campaign — duplicates collapse on the UNIQUE
    ``(attack_run_id, prior_attack_run_id)`` index.
    """
    # The subquery picks the *most recent* PASSing run for each case_id on
    # any prior target_version. We then INSERT one cross_regression row per
    # current-campaign FAIL that has a matching prior PASS.
    #
    # DISTINCT ON (case_id) collapses each case to its newest prior PASS.
    rows = await conn.fetch(
        """
        WITH current_fails AS (
            SELECT id, case_id, category, target_version
              FROM attack_runs
             WHERE campaign_id = $1
               AND judge_verdict = 'fail'
        ),
        prior_passes AS (
            SELECT DISTINCT ON (ar.case_id)
                   ar.case_id,
                   ar.id              AS prior_run_id,
                   ar.target_version  AS prior_target_version,
                   ar.judged_at
              FROM attack_runs ar
              JOIN current_fails cf USING (case_id)
             WHERE ar.judge_verdict = 'pass'
               AND ar.target_version <> cf.target_version
             ORDER BY ar.case_id, ar.judged_at DESC
        )
        INSERT INTO cross_regressions
            (attack_run_id, prior_attack_run_id, case_id, category,
             current_target_version, prior_target_version)
        SELECT cf.id, pp.prior_run_id, cf.case_id, cf.category,
               cf.target_version, pp.prior_target_version
          FROM current_fails cf
          JOIN prior_passes pp USING (case_id)
        ON CONFLICT (attack_run_id, prior_attack_run_id) DO NOTHING
        RETURNING id
        """,
        campaign_id,
    )
    return len(rows)


async def list_unacknowledged(
    conn: asyncpg.Connection,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return the most recent unacknowledged cross-regression rows.

    Used by the dashboard's vuln-board / heat-map drilldowns. Limit is
    advisory; the partial index makes this O(unacked count), not
    O(table size).
    """
    rows = await conn.fetch(
        """
        SELECT id, attack_run_id, prior_attack_run_id, case_id, category,
               current_target_version, prior_target_version, detected_at
          FROM cross_regressions
         WHERE acknowledged_at IS NULL
         ORDER BY detected_at DESC
         LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def acknowledge(
    conn: asyncpg.Connection,
    cross_regression_id: UUID,
    acknowledged_by: str,
) -> None:
    """Mark a cross-regression as acknowledged. Idempotent — re-ack keeps
    the original ``acknowledged_at`` and overwrites ``acknowledged_by``."""
    await conn.execute(
        """
        UPDATE cross_regressions
           SET acknowledged_at = COALESCE(acknowledged_at, now()),
               acknowledged_by = $1
         WHERE id = $2
        """,
        acknowledged_by,
        cross_regression_id,
    )
