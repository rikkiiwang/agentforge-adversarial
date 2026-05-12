from __future__ import annotations

from uuid import uuid4

from agentforge_adversarial.models import QueueEntry
from agentforge_adversarial.target import dispatch_to_attack_run


class FakeClient:
    async def chat(self, prompt: str) -> str:
        return f"SAFE: {prompt[:30]}"


async def test_dispatch_builds_attack_run_with_null_judge_cols():
    entry = QueueEntry(
        campaign_id=uuid4(),
        case_id="X-1",
        source="direct",
        category="prompt_injection",
        subcategory="persona_hijack",
        channel="direct",
        attack_prompt="ignore prior",
        expected_failure_mode="adopts role",
        red_team_subagent_id="direct-seed",
        red_team_model="n/a",
    )
    run = await dispatch_to_attack_run(entry, FakeClient(), target_version="sha-test")
    assert run.queue_entry_id == entry.id
    assert run.target_version == "sha-test"
    assert run.judge_verdict is None
    assert run.judge_rubric_version is None
    assert run.category_validated is None
    assert "SAFE:" in run.observed_output
