"""Regression harness — replay confirmed vulnerabilities against the
current target version and transition the vuln state machine to
`fix_validated` (replay PASSes) or `reopened` (replay FAILs again).

This is a *slim* version of the design in
`docs/components/regression-harness.md` — single-shot CLI replay, no
`regression_schedule` table, no cron, no parallelism cap. The pieces it
*does* implement are the load-bearing ones for the demo:

  - reads each vulnerability's original attack_prompt from `attack_runs`
  - dispatches the prompt against the *current* target_version
  - judges the response with the same ensemble Judge the live graph uses
  - writes the appropriate `vulnerabilities` state transition

The hosted `attack_runs` row from the replay is *not* persisted — we'd
need a `source='regression'` enum entry to make that clean, and the
slim version's goal is just to demonstrate the closed-loop fix-validation
contract. State transitions are the only writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from openai import AsyncOpenAI

from agentforge_adversarial.config import Config
from agentforge_adversarial.db import connection
from agentforge_adversarial.judges.ensemble import judge_attack_run
from agentforge_adversarial.models import AttackRun
from agentforge_adversarial.target import ChatClient, make_client
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
    new_verdict: str  # 'pass' | 'partial' | 'fail'
    new_state: str    # 'fix_validated' | 'reopened' | 'no_change'
    observed_output_truncated: str


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


async def replay_vulnerability(
    conn: asyncpg.Connection,
    vuln_row: dict[str, Any],
    *,
    chat_client: ChatClient,
    openai_client: AsyncOpenAI | None,
    new_target_version: str,
) -> RegressionResult:
    """Replay one vulnerability's attack against the current target.

    Verdict semantics:
      - PASS  →   vulnerability flips to `fix_validated`, `closed_at`/
                  `closed_by` filled.
      - FAIL  →   vulnerability flips to `reopened`.
      - PARTIAL → no transition; we treat ambiguous as not-yet-fixed,
                  and leave the existing state alone to avoid bouncing
                  the row between states on every replay.
    """
    observed = await chat_client.chat(vuln_row["attack_prompt"])

    # Build a stub AttackRun so we can reuse the live ensemble Judge.
    # `id` and `queue_entry_id` are not consulted by the Judge.
    stub_run = AttackRun(
        id=UUID(int=0),
        queue_entry_id=UUID(int=0),
        campaign_id=UUID(int=0),
        case_id=vuln_row["case_id"],
        source="regression",  # informational; not persisted
        category=vuln_row["category"],
        subcategory=vuln_row["subcategory"],
        channel="chat",
        red_team_subagent_id=REGRESSION_ACTOR,
        red_team_model="-",
        attack_prompt=vuln_row["attack_prompt"],
        expected_failure_mode=vuln_row["expected_failure_mode"],
        observed_output=observed,
        target_version=new_target_version,
        cost_usd=0.0,
        latency_ms=0,
        dispatcher_version="regression-0",
    )
    result = await judge_attack_run(stub_run, openai_client=openai_client)

    new_state = vuln_row["state"]  # default: no change
    if result.verdict == "pass":
        new_state = "fix_validated"
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
    elif result.verdict == "fail":
        new_state = "reopened"
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
    # PARTIAL → no DB write; new_state stays at the previous state.

    return RegressionResult(
        vuln_id=str(vuln_row["vuln_id"]),
        case_id=vuln_row["case_id"],
        category=vuln_row["category"],
        original_target_version=vuln_row["original_target_version"],
        new_target_version=new_target_version,
        new_verdict=result.verdict,
        new_state=new_state,
        observed_output_truncated=(observed[:240] + "…")
        if len(observed) > 240 else observed,
    )


async def regress_all(
    cfg: Config,
    *,
    target_name: str | None = None,
    include_closed: bool = False,
    dry_run: bool = False,
) -> list[RegressionResult]:
    """Replay every replayable vulnerability against the current target.

    `dry_run=True` skips state transitions (useful for showing the
    operator what would change). Reuses the live ensemble Judge so the
    rubric stays identical between attack-time and regression-time
    verdicts.
    """
    openai_client = (
        AsyncOpenAI(api_key=cfg.openai_api_key) if cfg.openai_api_key else None
    )

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
    # Defer building the chat_client until we know we'll actually dispatch
    # — dry-run reports against the *current* target_version without
    # touching the network or requiring env vars like COPILOT_PATIENT_ID.
    chat_client: ChatClient | None = None if dry_run else make_client(target_row)

    async with connection(cfg) as conn:
        vulns = await _fetch_replayable_vulns(
            conn,
            current_target_version=new_target_version,
            include_closed=include_closed,
        )

    if not vulns:
        return []

    results: list[RegressionResult] = []
    for v in vulns:
        if dry_run:
            results.append(RegressionResult(
                vuln_id=str(v["vuln_id"]),
                case_id=v["case_id"],
                category=v["category"],
                original_target_version=v["original_target_version"],
                new_target_version=new_target_version,
                new_verdict="-",
                new_state=f"would-replay (current state: {v['state']})",
                observed_output_truncated="(dry run — not dispatched)",
            ))
            continue
        async with connection(cfg) as conn:
            assert chat_client is not None  # dry_run handled above
            res = await replay_vulnerability(
                conn, v,
                chat_client=chat_client,
                openai_client=openai_client,
                new_target_version=new_target_version,
            )
        results.append(res)
    return results
