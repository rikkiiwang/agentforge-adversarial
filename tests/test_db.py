from __future__ import annotations

import os

import pytest

from agentforge_adversarial.config import Config
from agentforge_adversarial.db import connection

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ,
    reason="Live Postgres only — run after `docker compose up -d` and `init-db`",
)


async def test_tables_exist():
    async with connection(Config.from_env()) as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        )
    names = {r["table_name"] for r in rows}
    assert {"campaigns", "attack_queue", "attack_runs"} <= names


async def test_attack_runs_judge_atomic_check_present():
    """The atomic CHECK constraint should exist on attack_runs."""
    async with connection(Config.from_env()) as conn:
        row = await conn.fetchrow(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'attack_runs'::regclass AND conname = 'attack_runs_judge_atomic'
            """
        )
    assert row is not None, "attack_runs_judge_atomic CHECK is missing"
