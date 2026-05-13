"""Subprocess-based campaign launcher for the Streamlit operator console.

The dashboard imports this to spawn a campaign run from the browser. Streamlit
re-runs its script on every interaction, so we cannot block in `asyncio.run()`
inline — instead the campaign runs as a detached subprocess writing rows into
the same Postgres the dashboard reads. Progress is computed by counting rows.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agentforge_adversarial.cases import load_cases  # noqa: E402


def expected_row_count(cases_dir: Path) -> int:
    """Seeds + 3 mutations per seed (mutator is always on for live targets)."""
    seeds = load_cases(cases_dir)
    return len(seeds) * 4


def start_campaign(
    cases_dir: Path,
    *,
    target_name: str,
    env: dict[str, str] | None = None,
) -> tuple[int, UUID, int]:
    """Spawn `python -m agentforge_adversarial run --target <name> ...` and
    parse the campaign_id from stdout.

    Mutation is always on (the platform's value is the LLM-generated variant
    coverage). Targets come from the `targets` table; the operator picks one
    in the dashboard.

    Blocks briefly (up to 30s) while waiting for the runner to emit its
    `[campaign] created <uuid>` line. Returns (pid, campaign_id, expected_rows)
    so the caller can poll Postgres for progress.
    """
    cmd = [
        sys.executable,
        "-m",
        "agentforge_adversarial",
        "run",
        "--cases",
        str(cases_dir),
        "--target",
        target_name,
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            **(env or {}),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    campaign_id: UUID | None = None
    deadline = time.time() + 30
    assert proc.stdout is not None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        if "[campaign] created" in line:
            campaign_id = UUID(line.split()[2])
            break

    if campaign_id is None:
        proc.kill()
        raise RuntimeError(
            "Runner did not emit a campaign id within 30s. "
            "Check your environment (DATABASE_URL reachable? COPILOT_PATIENT_ID set?)."
        )

    return proc.pid, campaign_id, expected_row_count(cases_dir)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def campaign_progress(database_url: str, campaign_id: UUID) -> dict[str, object]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   max(judge_verdict::text),
                   max(created_at)
              FROM attack_runs
             WHERE campaign_id = %s
            """,
            (str(campaign_id),),
        )
        c, v, t = cur.fetchone()
    return {
        "count": int(c or 0),
        "last_verdict": v,
        "latest_created_at": t,
    }
