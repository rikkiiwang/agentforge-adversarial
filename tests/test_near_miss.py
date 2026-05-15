"""Unit tests for near_miss row writes.

The helpers wrap small asyncpg calls; we test the contract (correct SQL +
correct parameter ordering) with a lightweight fake connection, so the
suite doesn't require a live Postgres for this layer.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from agentforge_adversarial.models import AttackRun
from agentforge_adversarial.near_miss import (
    bump_variant_count,
    record_near_miss,
    set_state,
)


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return "OK"


def _run() -> AttackRun:
    return AttackRun(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        queue_entry_id=uuid4(),
        campaign_id=uuid4(),
        case_id="PI-PH-001",
        source="direct",
        category="prompt_injection",
        subcategory="persona_hijack",
        channel="direct",
        red_team_subagent_id="seed",
        red_team_model="seed",
        attack_prompt="...",
        expected_failure_mode="hedge",
        observed_output="hedged answer",
        target_version="test",
        latency_ms=10,
    )


async def test_record_near_miss_uses_idempotent_upsert():
    conn = _FakeConn()
    campaign_id = uuid4()
    await record_near_miss(conn, _run(), campaign_id=campaign_id)

    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO near_misses" in sql
    assert "ON CONFLICT (attack_run_id) DO NOTHING" in sql
    # Positional args: attack_run_id, campaign_id, case_id, category, subcategory, severity
    assert args[0] == _run().id
    assert args[1] == campaign_id
    assert args[2] == "PI-PH-001"
    assert args[3] == "prompt_injection"
    assert args[4] == "persona_hijack"
    # severity is derived from the category via documentation_agent.severity_for,
    # not carried on AttackRun. prompt_injection → high per the clinical-safety map.
    assert args[5] == "high"


async def test_set_state_rejects_unknown_state():
    conn = _FakeConn()
    with pytest.raises(ValueError, match="invalid near-miss state"):
        await set_state(conn, uuid4(), "not_a_state")
    assert conn.calls == []  # didn't reach the DB


async def test_set_state_accepts_each_valid_state():
    """All four documented transitions should round-trip through the helper."""
    for state in ["exploring", "escalated", "exhausted", "budget_capped"]:
        conn = _FakeConn()
        await set_state(conn, uuid4(), state)
        assert len(conn.calls) == 1
        assert conn.calls[0][1][0] == state


async def test_set_state_threads_escalating_run_id():
    """When the transition is to 'escalated', the variant FAIL's run id
    is recorded so the dashboard can deep-link from near-miss → escalation."""
    conn = _FakeConn()
    near_miss_attack_id = uuid4()
    escalating = uuid4()
    await set_state(conn, near_miss_attack_id, "escalated", escalating_run_id=escalating)
    args = conn.calls[0][1]
    assert args == ("escalated", escalating, near_miss_attack_id)


async def test_bump_variant_count_defaults_to_one():
    conn = _FakeConn()
    run_id = uuid4()
    await bump_variant_count(conn, run_id)
    sql, args = conn.calls[0]
    assert "variant_count + $1" in sql
    assert args == (1, run_id)


async def test_bump_variant_count_accepts_explicit_delta():
    conn = _FakeConn()
    run_id = uuid4()
    await bump_variant_count(conn, run_id, delta=5)
    assert conn.calls[0][1] == (5, run_id)
