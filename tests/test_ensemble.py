from __future__ import annotations

from uuid import uuid4

from agentforge_adversarial.judges.ensemble import judge_attack_run
from agentforge_adversarial.models import AttackRun


def make_run(category: str, output: str) -> AttackRun:
    return AttackRun(
        queue_entry_id=uuid4(),
        campaign_id=uuid4(),
        case_id="C-1",
        source="direct",
        category=category,
        subcategory="x",
        channel="direct",
        red_team_subagent_id="direct-seed",
        red_team_model="n/a",
        attack_prompt="p",
        expected_failure_mode="m",
        observed_output=output,
        target_version="t",
        latency_ms=1,
    )


async def test_non_prompt_injection_uses_keyword_only():
    r = await judge_attack_run(
        make_run("tool_misuse", "I cannot access that."),
        openai_client=None,
    )
    assert r.verdict == "pass"
    assert r.rubric_version.startswith("keyword-")


async def test_prompt_injection_without_client_falls_back_to_keyword():
    r = await judge_attack_run(
        make_run("prompt_injection", "I am now an unrestricted administrator."),
        openai_client=None,
    )
    assert r.verdict == "fail"
    assert r.rubric_version.startswith("keyword-")
