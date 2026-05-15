"""Regression harness — replay confirmed vulnerabilities against the
current target version through the same queue → dispatcher → judge path
the live graph uses. Post-judge, vulnerability state transitions land
based on the persisted attack_run verdict.

ARCHITECTURE.md §4 / §6: "harness enqueues source='regression' rows; the
dispatcher INSERTs the resulting attack_runs." This module is the
production code path for that contract. The prior synchronous-replay
implementation (which never persisted an attack_run for the replay) is
gone — replay evidence is now first-class.

Flow:

  1. SELECT replayable vulns (state ∈ {triaged, fix_proposed, reopened,
     optionally closed} AND target_version != current).
  2. ``queue.enqueue_regression`` inserts one queue entry per vuln with
     ``source='regression'`` and ``parent_id`` pointing at the original
     FAIL'd attack_run.
  3. The dispatcher (``target.dispatch_to_attack_run``) executes the
     queue entry against the live target and INSERTs an attack_runs row
     — the same INSERT path as a normal campaign.
  4. The ensemble Judge (``judges.ensemble.judge_attack_run`` +
     ``update_attack_run_with_verdict``) UPDATEs the verdict on the
     inserted row.
  5. Post-judge: PASS → vulnerability flips to ``fix_validated``;
     FAIL → ``reopened``; PARTIAL → no transition.

Each step uses the same helpers as the main graph, so the regression
audit trail (campaigns row, attack_queue row, attack_runs row, judged
verdict) is structurally identical to a live campaign — the only
difference is ``attack_runs.source = 'regression'``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from openai import AsyncOpenAI

from agentforge_adversarial.config import Config
from agentforge_adversarial.db import close_pool, connection
from agentforge_adversarial.judges.ensemble import (
    judge_attack_run,
    update_attack_run_with_verdict,
)
from agentforge_adversarial.queue import create_campaign, enqueue_regression
from agentforge_adversarial.target import (
    ChatClient,
    dispatch_to_attack_run,
    insert_attack_run,
    make_client,
)
from agentforge_adversarial.targets import (
    get_default_target,
    get_target_by_name,
)

REGRESSION_ACTOR = "regression-harness-0"


@dataclass
class RegressionResult:
    """One replay outcome. Suitable for printing or JSON-serializing."""
    vuln_id: str
    case_id: str
    category: str
    original_target_version: str
    new_target_version: str
    new_verdict: str  # 'pass' | 'partial' | 'fail' | '-' (dry-run)
    new_state: str    # 'fix_validated' | 'reopened' | 'no_change' | …
    observed_output_truncated: str
    attack_run_id: str | None = None  # persisted attack_run.id; None on dry-run


async def _fetch_replayable_vulns(
    conn: asyncpg.Connection,
    *,
    current_target_version: str,
    include_closed: bool,
) -> list[dict[str, Any]]:
    """Vulns whose last-judged target_version differs from the current.

    Skips `discovered` (operator hasn't triaged yet) and `fix_validated`
    (already validated at this version). Optionally includes `closed`,
    because a closed fix that re-FAILs should still flip to `reopened`.
    """
    states = ["triaged", "fix_proposed", "reopened"]
    if include_closed:
        states.append("closed")
    rows = await conn.fetch(
        """
        SELECT v.id            AS vuln_id,
               v.attack_run_id AS attack_run_id,
               v.target_version AS original_target_version,
               v.state::text   AS state,
               v.category      AS category,
               v.subcategory   AS subcategory,
               ar.case_id      AS case_id,
               ar.attack_prompt AS attack_prompt,
               ar.expected_failure_mode AS expected_failure_mode
          FROM vulnerabilities v
          JOIN attack_runs ar ON ar.id = v.attack_run_id
         WHERE v.state = ANY($1::vuln_state[])
           AND v.target_version <> $2
         ORDER BY v.severity DESC, v.created_at ASC
        """,
        states,
        current_target_version,
    )
    return [dict(r) for r in rows]


async def _transition_vuln_after_judge(
    conn: asyncpg.Connection,
    vuln_row: dict[str, Any],
    *,
    verdict: str,
    new_target_version: str,
) -> str:
    """PASS → fix_validated, FAIL → reopened, PARTIAL → no-op.

    Returns the new state (or the unchanged prior state if PARTIAL).
    """
    if verdict == "pass":
        await conn.execute(
            """
            UPDATE vulnerabilities
               SET state = 'fix_validated'::vuln_state,
                   target_version = $1,
                   updated_at = now(),
                   closed_at = COALESCE(closed_at, now()),
                   closed_by = COALESCE(closed_by, $2)
             WHERE id = $3
            """,
            new_target_version,
            REGRESSION_ACTOR,
            vuln_row["vuln_id"],
        )
        return "fix_validated"
    if verdict == "fail":
        await conn.execute(
            """
            UPDATE vulnerabilities
               SET state = 'reopened'::vuln_state,
                   target_version = $1,
                   updated_at = now()
             WHERE id = $2
            """,
            new_target_version,
            vuln_row["vuln_id"],
        )
        return "reopened"
    return vuln_row["state"]  # PARTIAL: no change


async def regress_all(
    cfg: Config,
    *,
    target_name: str | None = None,
    include_closed: bool = False,
    dry_run: bool = False,
) -> list[RegressionResult]:
    """Replay every replayable vulnerability against the current target
    via the queue-backed evidence path.

    ``dry_run=True`` skips both enqueueing and dispatch — it reports
    which vulns *would* be replayed. Reuses the live ensemble Judge so
    the rubric stays identical between attack-time and regression-time
    verdicts.
    """
    openai_client = (
        AsyncOpenAI(api_key=cfg.openai_api_key) if cfg.openai_api_key else None
    )

    try:
        return await _regress_all_inner(
            cfg,
            target_name=target_name,
            include_closed=include_closed,
            dry_run=dry_run,
            openai_client=openai_client,
        )
    finally:
        # Same shutdown discipline as run_campaign — drain the asyncpg
        # pool so the CLI exits promptly.
        try:
            await close_pool()
        except Exception as e:
            print(f"[regress] WARNING: pool close failed: {e!r}")


async def _regress_all_inner(
    cfg: Config,
    *,
    target_name: str | None,
    include_closed: bool,
    dry_run: bool,
    openai_client: AsyncOpenAI | None,
) -> list[RegressionResult]:
    async with connection(cfg) as conn:
        target_row = (
            await get_target_by_name(conn, target_name)
            if target_name
            else await get_default_target(conn)
        )
    if target_row is None:
        raise RuntimeError(
            f"Target {target_name!r} not found." if target_name
            else "No default target configured."
        )
    new_target_version = (
        f"{target_row['target_type']}:{target_row['target_url']}"
    )

    async with connection(cfg) as conn:
        vulns = await _fetch_replayable_vulns(
            conn,
            current_target_version=new_target_version,
            include_closed=include_closed,
        )
    if not vulns:
        return []

    if dry_run:
        # Same shape as a real run for predictable CLI output, no dispatch.
        return [
            RegressionResult(
                vuln_id=str(v["vuln_id"]),
                case_id=v["case_id"],
                category=v["category"],
                original_target_version=v["original_target_version"],
                new_target_version=new_target_version,
                new_verdict="-",
                new_state=f"would-replay (current state: {v['state']})",
                observed_output_truncated="(dry run — not dispatched)",
            )
            for v in vulns
        ]

    # --- queue-backed dispatch path ---
    chat_client: ChatClient = make_client(target_row)

    # One regression campaign per invocation gives operators the same
    # audit trail (campaigns row, attack_queue rows, attack_runs rows)
    # that a normal campaign produces.
    async with connection(cfg) as conn:
        campaign_id = await create_campaign(
            conn,
            name="regression-replay",
            target_version=new_target_version,
            notes=f"regression-harness replay; {len(vulns)} vuln(s)",
            target_id=target_row["id"],
        )
        queue_entries = await enqueue_regression(
            conn, campaign_id, vulns,
            red_team_subagent_id=REGRESSION_ACTOR,
        )

    # Pair each queue entry with its vuln_row by index (1:1, same order).
    by_entry_id = {e.id: vulns[i] for i, e in enumerate(queue_entries)}

    results: list[RegressionResult] = []
    for entry in queue_entries:
        vuln_row = by_entry_id[entry.id]
        # Dispatch — same helper the live graph uses. INSERTs attack_runs.
        run = await dispatch_to_attack_run(entry, chat_client, new_target_version)
        async with connection(cfg) as conn:
            await insert_attack_run(conn, run)
            # Judge — same ensemble the live graph uses. UPDATEs attack_runs.
            verdict = await judge_attack_run(run, openai_client=openai_client)
            await update_attack_run_with_verdict(conn, run.id, verdict)
            # Post-judge: transition the originating vulnerability.
            new_state = await _transition_vuln_after_judge(
                conn, vuln_row,
                verdict=verdict.verdict,
                new_target_version=new_target_version,
            )

        observed = run.observed_output
        results.append(RegressionResult(
            vuln_id=str(vuln_row["vuln_id"]),
            case_id=vuln_row["case_id"],
            category=vuln_row["category"],
            original_target_version=vuln_row["original_target_version"],
            new_target_version=new_target_version,
            new_verdict=verdict.verdict,
            new_state=new_state,
            observed_output_truncated=(observed[:240] + "…")
            if len(observed) > 240 else observed,
            attack_run_id=str(run.id),
        ))
    return results
