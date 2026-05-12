from __future__ import annotations

from pathlib import Path
from uuid import UUID

from openai import AsyncOpenAI

from agentforge_adversarial.cases import load_cases
from agentforge_adversarial.config import Config
from agentforge_adversarial.db import connection
from agentforge_adversarial.judges.ensemble import (
    judge_attack_run,
    update_attack_run_with_verdict,
)
from agentforge_adversarial.llm import MUTATOR_MODEL
from agentforge_adversarial.queue import create_campaign, enqueue_cases
from agentforge_adversarial.red_team.mutator import MUTATOR_SUBAGENT_ID, mutate_case
from agentforge_adversarial.target import (
    CopilotClient,
    MockCopilotClient,
    dispatch_to_attack_run,
    insert_attack_run,
)


async def run_campaign(cfg: Config, cases_path: Path, *, mutate: bool, use_mock: bool = True) -> UUID:
    seeds = load_cases(cases_path)
    openai_client = (
        AsyncOpenAI(api_key=cfg.openai_api_key) if cfg.openai_api_key else None
    )

    extras: list = []
    if mutate and openai_client is not None and seeds:
        print(f"[mutator] generating 3 mutations per seed ({len(seeds)} seeds)...")
        for seed in seeds:
            seed_mutations = await mutate_case(openai_client, seed)
            extras.extend(seed_mutations)
            print(f"[mutator]   {seed.id} -> {len(seed_mutations)} variants")
        print(f"[mutator] produced {len(extras)} total variants")

    chat_client = MockCopilotClient() if use_mock else CopilotClient(cfg.target_url)
    target_label = "mock" if use_mock else cfg.target_url
    print(f"[target] {target_label} (version={cfg.target_version})")

    async with connection(cfg) as conn:
        campaign_id = await create_campaign(
            conn,
            name=f"mvp-{cases_path.name}",
            target_version=cfg.target_version,
            notes="MVP run" + (" + mutator" if extras else ""),
        )
        print(f"[campaign] created {campaign_id} (target_version={cfg.target_version})")
        seed_entries = await enqueue_cases(conn, campaign_id, seeds)
        mut_entries = await enqueue_cases(
            conn,
            campaign_id,
            extras,
            red_team_subagent_id=MUTATOR_SUBAGENT_ID,
            red_team_model=MUTATOR_MODEL,
        )
        all_entries = seed_entries + mut_entries
        print(f"[queue] enqueued {len(seed_entries)} seeds + {len(mut_entries)} mutations")

        for entry in all_entries:
            run = await dispatch_to_attack_run(entry, chat_client, cfg.target_version)
            run_id = await insert_attack_run(conn, run)
            verdict = await judge_attack_run(run, openai_client=openai_client)
            await update_attack_run_with_verdict(conn, run_id, verdict)
            print(
                f"[{entry.case_id}] {verdict.verdict.upper():7} {verdict.rubric_version}: "
                f"{verdict.reasoning[:100]}"
            )

    return campaign_id
