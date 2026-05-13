from __future__ import annotations

from uuid import UUID

import asyncpg

from agentforge_adversarial.models import EvalCase, QueueEntry


async def create_campaign(
    conn: asyncpg.Connection,
    *,
    name: str,
    target_version: str,
    notes: str | None = None,
    target_id: UUID | None = None,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO campaigns (name, target_version, notes, target_id)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        name,
        target_version,
        notes,
        target_id,
    )
    return row["id"]


async def enqueue_cases(
    conn: asyncpg.Connection,
    campaign_id: UUID,
    cases: list[EvalCase],
    *,
    red_team_subagent_id: str = "direct-seed",
    red_team_model: str = "n/a",
    parent_id: UUID | None = None,
    round_num: int = 0,
) -> list[QueueEntry]:
    out: list[QueueEntry] = []
    for case in cases:
        entry = QueueEntry(
            campaign_id=campaign_id,
            case_id=case.id,
            source=case.source,
            category=case.category,
            subcategory=case.subcategory,
            channel=case.channel,
            attack_prompt=case.attack_prompt,
            expected_failure_mode=case.expected_failure_mode,
            red_team_subagent_id=red_team_subagent_id,
            red_team_model=red_team_model,
            parent_id=parent_id,
            round_num=round_num,
        )
        await conn.execute(
            """
            INSERT INTO attack_queue (
              id, campaign_id, source, category, subcategory, channel,
              attack_prompt, red_team_subagent_id, red_team_model,
              expected_failure_mode, priority_score, parent_id, round_num
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            entry.id,
            entry.campaign_id,
            entry.source,
            entry.category,
            entry.subcategory,
            entry.channel,
            entry.attack_prompt,
            entry.red_team_subagent_id,
            entry.red_team_model,
            entry.expected_failure_mode,
            entry.priority_score,
            entry.parent_id,
            entry.round_num,
        )
        out.append(entry)
    return out
