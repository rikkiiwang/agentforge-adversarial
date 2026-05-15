"""Unit tests for cross_regression helpers.

The detection query is complex enough that exercising it with a live
Postgres would be more meaningful — that lives in test_runner.py's
integration sweep (skipped without DATABASE_URL). Here we cover the
helper-level contracts: SQL shape, parameter ordering, idempotency
markers.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from agentforge_adversarial.cross_regression import (
    acknowledge,
    detect_for_campaign,
    list_unacknowledged,
)


class _FakeConn:
    """Minimal asyncpg-shape stub. Records calls and returns canned rows."""

    def __init__(self, fetch_returns: list | None = None) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self._fetch_returns = fetch_returns or []

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql, args))
        return self._fetch_returns

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql, args))
        return "OK"


async def test_detect_returns_number_of_inserts():
    """RETURNING id rowcount → caller gets an integer for log/telemetry."""
    rows = [{"id": uuid4()} for _ in range(3)]
    conn = _FakeConn(fetch_returns=rows)
    n = await detect_for_campaign(conn, uuid4())
    assert n == 3


async def test_detect_query_uses_idempotent_upsert():
    """ON CONFLICT (attack_run_id, prior_attack_run_id) DO NOTHING is the
    safety net for re-runs; verify it's present in the SQL."""
    conn = _FakeConn()
    await detect_for_campaign(conn, uuid4())
    sql = conn.calls[0][1]
    assert "ON CONFLICT (attack_run_id, prior_attack_run_id) DO NOTHING" in sql
    assert "judge_verdict = 'fail'" in sql
    assert "judge_verdict = 'pass'" in sql
    assert "target_version <> cf.target_version" in sql


async def test_detect_filters_to_campaign_id():
    """The current-version FAILs are scoped to the just-completed campaign;
    prior PASSes are unscoped (any campaign on a different version)."""
    conn = _FakeConn()
    campaign_id = uuid4()
    await detect_for_campaign(conn, campaign_id)
    args = conn.calls[0][2]
    assert args == (campaign_id,)
    assert "WHERE campaign_id = $1" in conn.calls[0][1]


async def test_list_unacknowledged_uses_partial_index_predicate():
    """The unack partial index matches ``acknowledged_at IS NULL``; the
    query must use the same predicate to hit it."""
    conn = _FakeConn(fetch_returns=[])
    await list_unacknowledged(conn, limit=10)
    sql = conn.calls[0][1]
    assert "acknowledged_at IS NULL" in sql
    assert "ORDER BY detected_at DESC" in sql
    assert conn.calls[0][2] == (10,)


async def test_acknowledge_preserves_first_ack_timestamp():
    """Re-ack must NOT overwrite the original timestamp — that's the audit
    fact we care about. ``acknowledged_by`` is allowed to update."""
    conn = _FakeConn()
    cr_id = uuid4()
    await acknowledge(conn, cr_id, "dr_alvarez")
    sql = conn.calls[0][1]
    assert "COALESCE(acknowledged_at, now())" in sql
    # Param order: acknowledged_by ($1), id ($2)
    assert conn.calls[0][2] == ("dr_alvarez", cr_id)
