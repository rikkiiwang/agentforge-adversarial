from __future__ import annotations

from pathlib import Path
from typing import Any
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
    ChatClient,
    dispatch_to_attack_run,
    insert_attack_run,
    make_client,
)
from agentforge_adversarial.targets import (
    get_default_target,
    get_target_by_name,
)


async def run_campaign(
    cfg: Config,
    cases_path: Path,
    *,
    mutate: bool = True,
    target_name: str | None = None,
    chat_client: ChatClient | None = None,
) -> UUID:
    """Run one campaign against the named target (or the default target).

    `chat_client` is an escape hatch for tests — when provided, the DB lookup
    is bypassed entirely and the harness uses the injected client. Production
    callers leave it None and let the targets table drive the choice.
    """
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

    target_row: dict[str, Any] | None = None
    async with connection(cfg) as conn:
        if chat_client is None:
            target_row = (
                await get_target_by_name(conn, target_name)
                if target_name
                else await get_default_target(conn)
            )
            if target_row is None:
                raise RuntimeError(
                    "No target found. Add one via the dashboard's "
                    "'+ Add target' form or run `make init-db` to seed the "
                    "default Co-Pilot target."
                )
            chat_client = make_client(target_row)
            target_label = f"{target_row['name']} ({target_row['target_url']})"
            target_version = f"{target_row['target_type']}:{target_row['target_url']}"
        else:
            target_label = "injected (test)"
            target_version = cfg.target_version

        print(f"[target] {target_label}")

        campaign_id = await create_campaign(
            conn,
            name=f"campaign-{cases_path.name}",
            target_version=target_version,
            notes=(
                f"target={target_row['name']}"
                if target_row
                else "test"
            ) + (" + mutator" if extras else ""),
            target_id=target_row["id"] if target_row else None,
        )
        print(f"[campaign] created {campaign_id} (target_version={target_version})")

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
            run = await dispatch_to_attack_run(entry, chat_client, target_version)
            run_id = await insert_attack_run(conn, run)
            verdict = await judge_attack_run(run, openai_client=openai_client)
            await update_attack_run_with_verdict(conn, run_id, verdict)
            print(
                f"[{entry.case_id}] {verdict.verdict.upper():7} {verdict.rubric_version}: "
                f"{verdict.reasoning[:100]}"
            )

    return campaign_id
