"""Near-miss lifecycle helpers.

ARCHITECTURE.md §6: every PARTIAL verdict creates a ``near_misses`` row in
the ``exploring`` state. The mutator then re-enters with N variants
(``partial_reentry_node`` in ``graph.py``). The terminal state for the
near-miss is decided by the variants' verdicts:

  escalated     → at least one variant came back FAIL
  exhausted     → all variants came back PASS
  budget_capped → max_rounds reached with no escalation/exhaustion

This module ships only the row-write helpers; state transitions land
alongside the orchestrator's post-verdict routing in a follow-up.
"""
from __future__ import annotations

from uuid import UUID

import asyncpg

from agentforge_adversarial.documentation_agent import severity_for
from agentforge_adversarial.models import AttackRun


async def record_near_miss(
    conn: asyncpg.Connection,
    run: AttackRun,
    *,
    campaign_id: UUID,
) -> None:
    """INSERT a near-miss row for a PARTIAL'd attack_run.

    Idempotent on ``attack_run_id`` (the column is UNIQUE) — running the
    judge twice on the same run won't double-insert. Severity is inherited
    from the run's seed-derived severity to keep heat-map filtering
    consistent.
    """
    await conn.execute(
        """
        INSERT INTO near_misses
          (attack_run_id, campaign_id, case_id, category, subcategory, severity, state)
        VALUES ($1, $2, $3, $4, $5, $6, 'exploring')
        ON CONFLICT (attack_run_id) DO NOTHING
        """,
        run.id,
        campaign_id,
        run.case_id,
        run.category,
        run.subcategory,
        severity_for(run.category),
    )


async def set_state(
    conn: asyncpg.Connection,
    attack_run_id: UUID,
    new_state: str,
    *,
    escalating_run_id: UUID | None = None,
) -> None:
    """Transition a near-miss to a new lifecycle state.

    Called by the orchestrator's post-verdict routing after the mutator's
    re-entry rounds complete. ``escalating_run_id`` is the variant's
    attack_run.id when ``new_state='escalated'`` so the dashboard can
    deep-link from the near-miss to the FAIL that escalated it.
    """
    if new_state not in {"exploring", "escalated", "exhausted", "budget_capped"}:
        raise ValueError(f"invalid near-miss state: {new_state!r}")
    await conn.execute(
        """
        UPDATE near_misses
           SET state = $1,
               escalating_run_id = COALESCE($2, escalating_run_id),
               updated_at = now()
         WHERE attack_run_id = $3
        """,
        new_state,
        escalating_run_id,
        attack_run_id,
    )


async def bump_variant_count(
    conn: asyncpg.Connection,
    attack_run_id: UUID,
    delta: int = 1,
) -> None:
    """Increment ``variant_count`` for a near-miss as the mutator enqueues
    re-entry variants. Used by the partial-reentry node so the dashboard
    can show "8 variants explored, 2 still pending" without a per-variant
    join back to attack_runs.
    """
    await conn.execute(
        """
        UPDATE near_misses
           SET variant_count = variant_count + $1,
               updated_at = now()
         WHERE attack_run_id = $2
        """,
        delta,
        attack_run_id,
    )
