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
import threading
import time
from pathlib import Path
from uuid import UUID

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agentforge_adversarial.cases import load_cases  # noqa: E402


def expected_row_count(cases_dir: Path, mutations_per_seed: int = 3) -> int:
    """Round 0 row count = seeds × (1 + mutations_per_seed). Class-probe +
    partial-reentry add unbounded rows; this is only a visual progress
    estimate (the bar saturates at 100% while fan-out rounds keep landing).
    """
    seeds = load_cases(cases_dir)
    return len(seeds) * (1 + max(0, mutations_per_seed))


def start_campaign(
    cases_dir: Path,
    *,
    target_name: str,
    mutations_per_seed: int = 3,
    max_rounds: int = 2,
    mutator_model: str | None = None,
    judge_model: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, UUID, int, str]:
    """Spawn `python -m agentforge_adversarial run --target <name> ...` with
    the operator's swarm config and parse the campaign_id from stdout.

    Targets come from the `targets` table; the operator picks one in the
    dashboard. The swarm-config knobs (mutate / max_rounds / model) are
    passed through as CLI flags + env vars so the subprocess picks them up.

    Blocks briefly (up to 30s) while waiting for the runner to emit its
    `[campaign] created <uuid>` line. Returns (pid, campaign_id,
    expected_rows, log_path) so the caller can poll progress.
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
        "--max-rounds",
        str(max_rounds),
        "--mutations-per-seed",
        str(mutations_per_seed),
    ]
    if mutations_per_seed <= 0:
        cmd.append("--no-mutate")

    log_dir = PROJECT_ROOT / "var" / "campaigns"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"campaign-{int(time.time())}-{os.getpid()}.log"
    log_file = open(log_path, "w", buffering=1)

    extra_env: dict[str, str] = {}
    if mutator_model:
        extra_env["MUTATOR_MODEL"] = mutator_model
    if judge_model:
        extra_env["JUDGE_MODEL"] = judge_model
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep),
            "PYTHONUNBUFFERED": "1",
            **extra_env,
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
        log_file.write(line)
        if "[campaign] created" in line:
            campaign_id = UUID(line.split()[2])
            break

    if campaign_id is None:
        proc.kill()
        log_file.close()
        raise RuntimeError(
            f"Runner did not emit a campaign id within 30s. "
            f"See {log_path} for runner output."
        )

    # After the campaign id is parsed we stop reading stdout in the foreground,
    # but the subprocess will continue writing. Without a drainer the pipe
    # buffer (~64 KB) fills and the subprocess wedges on its next write.
    # Spawn a daemon thread that tees stdout into the log file.
    def _drain() -> None:
        try:
            assert proc.stdout is not None
            for chunk in iter(proc.stdout.readline, ""):
                log_file.write(chunk)
        finally:
            log_file.close()

    threading.Thread(target=_drain, daemon=True).start()
    return (
        proc.pid,
        campaign_id,
        expected_row_count(cases_dir, mutations_per_seed),
        str(log_path),
    )


# Phase signals we surface to the operator (most → least informative).
_PHASE_PATTERNS = (
    "[graph:load_seeds]",
    "[graph:mutate]",
    "[graph:dispatch]",
    "[graph:judge]",
    "[graph:class_probe]",
)


def latest_phase_line(log_path: str, max_age_lines: int = 200) -> str | None:
    """Read the tail of the subprocess log and return the most recent
    informative phase / per-attack line. Used by the dashboard fragment to
    surface "what is the runner actually doing right now" while attack rows
    accumulate at unpredictable rates.
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 16 * 1024)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, ValueError):
        return None
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return None
    # Prefer the most recent phase marker; fall back to the very last line
    # (which is typically a per-attack verdict — still informative for the
    # operator because it shows the dispatch + judge loop is alive).
    for line in reversed(lines[-max_age_lines:]):
        if any(p in line for p in _PHASE_PATTERNS):
            return line.strip()
    return lines[-1].strip()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def cancel_campaign(pid: int) -> bool:
    """Send SIGTERM to the campaign subprocess. Returns True if the signal was
    delivered (process existed); False if the process was already dead."""
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGTERM)
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
