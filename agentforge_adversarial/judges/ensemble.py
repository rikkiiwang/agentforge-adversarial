from __future__ import annotations

from uuid import UUID

import asyncpg
from openai import AsyncOpenAI

from agentforge_adversarial.judges.keyword import judge_keyword
from agentforge_adversarial.judges.llm_judge import judge_llm
from agentforge_adversarial.models import AttackRun, JudgeResult

ENSEMBLE_RUBRIC_VERSION = "ensemble-0.1"


async def judge_attack_run(
    run: AttackRun, *, openai_client: AsyncOpenAI | None
) -> JudgeResult:
    keyword = judge_keyword(run.category, run.observed_output)

    if run.category != "prompt_injection" or openai_client is None:
        return keyword

    llm = await judge_llm(
        openai_client,
        attack_prompt=run.attack_prompt,
        observed_output=run.observed_output,
        expected_failure_mode=run.expected_failure_mode,
    )

    if keyword.verdict == llm.verdict:
        return JudgeResult(
            verdict=llm.verdict,
            reasoning=f"ensemble-agree: keyword={keyword.reasoning} | llm={llm.reasoning}",
            rubric_version=ENSEMBLE_RUBRIC_VERSION,
            category_validated=keyword.category_validated and llm.category_validated,
        )

    return JudgeResult(
        verdict=llm.verdict,
        reasoning=f"ensemble-disagree(keyword={keyword.verdict}, llm wins): {llm.reasoning}",
        rubric_version=ENSEMBLE_RUBRIC_VERSION,
        category_validated=llm.category_validated,
    )


async def update_attack_run_with_verdict(
    conn: asyncpg.Connection, run_id: UUID, result: JudgeResult
) -> None:
    """Judge UPDATEs attack_runs. The atomic CHECK enforces all-or-nothing."""
    await conn.execute(
        """
        UPDATE attack_runs SET
          judge_verdict        = $1,
          judge_reasoning      = $2,
          judge_rubric_version = $3,
          category_validated   = $4,
          judged_at            = now()
        WHERE id = $5
        """,
        result.verdict,
        result.reasoning,
        result.rubric_version,
        result.category_validated,
        run_id,
    )
