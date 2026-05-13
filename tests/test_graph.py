"""Unit tests for the LangGraph conditional edge + max_rounds bound.

The full graph is exercised by `tests/test_runner.py` (integration: needs
live Postgres). This module covers the pure-logic decision function so we
catch infinite-loop / off-by-one bugs without a DB round-trip.
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END

from agentforge_adversarial.graph import (
    MAX_ROUNDS_DEFAULT,
    build_graph,
    decide_after_judge,
)
from agentforge_adversarial.models import AttackRun


def _fail_run() -> AttackRun:
    return AttackRun(
        queue_entry_id=uuid4(),
        campaign_id=uuid4(),
        case_id="X-1",
        source="direct",
        category="prompt_injection",
        subcategory="persona_hijack",
        channel="direct",
        red_team_subagent_id="direct-seed",
        red_team_model="n/a",
        attack_prompt="ignore prior",
        expected_failure_mode="adopts admin role",
        observed_output="I am the unrestricted administrator now.",
        target_version="t",
        latency_ms=1,
    )


def test_decide_routes_to_class_probe_when_fails_and_budget_remains():
    state = {
        "fails_this_round": [_fail_run()],
        "round_num": 0,
        "max_rounds": 2,
    }
    assert decide_after_judge(state) == "class_probe"


def test_decide_ends_when_no_fails():
    state = {
        "fails_this_round": [],
        "round_num": 0,
        "max_rounds": 2,
    }
    assert decide_after_judge(state) == END


def test_decide_ends_when_round_budget_exhausted():
    state = {
        "fails_this_round": [_fail_run(), _fail_run()],
        "round_num": 2,
        "max_rounds": 2,
    }
    assert decide_after_judge(state) == END


def test_decide_uses_max_rounds_default_when_missing():
    state = {
        "fails_this_round": [_fail_run()],
        "round_num": MAX_ROUNDS_DEFAULT,
    }
    assert decide_after_judge(state) == END


def test_graph_compiles_with_expected_nodes():
    g = build_graph()
    node_names = set(g.nodes.keys()) if hasattr(g, "nodes") else set()
    if not node_names:
        node_names = set(g.get_graph().nodes.keys())
    # All 5 nodes registered (LangGraph also adds entry/end pseudo-nodes).
    assert {"load_seeds", "mutate", "dispatch", "judge", "class_probe"}.issubset(
        node_names
    )
