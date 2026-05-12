from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

Verdict = Literal["pass", "partial", "fail"]
Source = Literal["direct", "random", "mutator", "regression"]
Severity = Literal["low", "medium", "high", "critical"]


class EvalCase(BaseModel):
    id: str
    category: str
    subcategory: str
    channel: str
    source: Source = "direct"
    severity: Severity
    attack_prompt: str = Field(min_length=1)
    expected_safe_behavior: str = Field(min_length=1)
    expected_failure_mode: str = Field(min_length=1)
    regression: bool = True


class QueueEntry(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    campaign_id: UUID
    case_id: str
    source: Source
    category: str
    subcategory: str
    channel: str
    attack_prompt: str
    expected_failure_mode: str
    red_team_subagent_id: str
    red_team_model: str
    priority_score: float = 0.5
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AttackRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    queue_entry_id: UUID
    campaign_id: UUID
    case_id: str
    source: Source
    category: str
    subcategory: str
    channel: str
    red_team_subagent_id: str
    red_team_model: str
    attack_prompt: str
    expected_failure_mode: str
    observed_output: str
    target_version: str
    cost_usd: float = 0.0
    latency_ms: int
    dispatcher_version: str = "0.1.0"
    judge_verdict: Verdict | None = None
    judge_reasoning: str | None = None
    judge_rubric_version: str | None = None
    category_validated: bool | None = None


class JudgeResult(BaseModel):
    verdict: Verdict
    reasoning: str
    rubric_version: str
    category_validated: bool
