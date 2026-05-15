"""Campaign entry point. Compiles + invokes the LangGraph state machine.

The straight-line for-loop that lived here in the MVP has moved into
`graph.py` as 5 nodes + 1 conditional edge. This module now just resolves
the target row, builds the chat client, and hands off to the graph.

Test escape hatch: pass `chat_client=<FakeClient>` to skip the targets-table
lookup. Production callers leave it None.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from agentforge_adversarial.config import Config
from agentforge_adversarial.cross_regression import detect_for_campaign
from agentforge_adversarial.db import close_pool, connection
from agentforge_adversarial import cost, observability
from agentforge_adversarial.graph import MAX_ROUNDS_DEFAULT, build_graph
from agentforge_adversarial.llm import MUTATOR_MODEL, is_anthropic_model
from agentforge_adversarial.target import ChatClient, auto_pick_patient_id, make_client
from agentforge_adversarial.targets import (
    get_default_target,
    get_target_by_name,
    patch_target_config,
)


async def run_campaign(
    cfg: Config,
    cases_path: Path,
    *,
    mutate: bool = True,
    mutations_per_seed: int = 3,
    target_name: str | None = None,
    chat_client: ChatClient | None = None,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
) -> UUID:
    openai_client = (
        AsyncOpenAI(api_key=cfg.openai_api_key) if cfg.openai_api_key else None
    )
    # Red Team client: split from Judge by model family (ARCHITECTURE §2).
    # Picks the provider that matches ``MUTATOR_MODEL``; falls back to None
    # so the graph's mutate / class-probe nodes skip themselves cleanly when
    # the required key is absent (same degradation path as ``openai_client``).
    if is_anthropic_model(MUTATOR_MODEL):
        red_team_client: AsyncOpenAI | AsyncAnthropic | None = (
            AsyncAnthropic(api_key=cfg.anthropic_api_key)
            if cfg.anthropic_api_key else None
        )
    else:
        red_team_client = openai_client

    target_row: dict[str, Any]
    if chat_client is None:
        async with connection(cfg) as conn:
            row = (
                await get_target_by_name(conn, target_name)
                if target_name
                else await get_default_target(conn)
            )
        if row is None:
            raise RuntimeError(
                "No target found. Add one via the dashboard's '+ Add target' "
                "form or run `make init-db` to seed the default Co-Pilot target."
            )
        target_row = row
        # Auto-pick patient_id on first launch for Co-Pilot targets that haven't
        # been configured yet. Removes the manual UUID copy step from the
        # README's "Run a campaign — deployed Co-Pilot target" recipe. Best-effort
        # — if the endpoint is missing/empty/unreachable, make_client below will
        # raise the existing RuntimeError pointing at the env-var fallback.
        if (
            target_row["target_type"] == "copilot"
            and not (target_row.get("config_json") or {}).get("patient_id")
        ):
            picked = await auto_pick_patient_id(target_row["target_url"])
            if picked:
                async with connection(cfg) as conn:
                    await patch_target_config(
                        conn, target_row["id"], {"patient_id": picked}
                    )
                cfg_json = dict(target_row.get("config_json") or {})
                cfg_json["patient_id"] = picked
                target_row["config_json"] = cfg_json
                print(f"[target] auto-picked patient_id={picked[:12]}…")
        chat_client = make_client(target_row)
        target_label = f"{target_row['name']} ({target_row['target_url']})"
        target_version = f"{target_row['target_type']}:{target_row['target_url']}"
    else:
        target_row = {
            "id": None,
            "name": "injected",
            "target_type": "test",
            "target_url": "test://",
            "config_json": {},
        }
        target_label = "injected (test)"
        target_version = cfg.target_version

    print(f"[target] {target_label}")

    graph = build_graph()
    initial: dict[str, Any] = {
        "cfg": cfg,
        "target_row": target_row,
        "target_version": target_version,
        "cases_path": cases_path,
        "mutate": mutate,
        "mutations_per_seed": mutations_per_seed,
        "max_rounds": max_rounds,
        "openai_client": openai_client,
        "red_team_client": red_team_client,
        "chat_client": chat_client,
    }
    try:
        final_state = await graph.ainvoke(initial)
        campaign_id = final_state["campaign_id"]
        # Flush per-campaign cost rollup. Failure here must not mask the
        # campaign's primary result, so log + continue rather than re-raise.
        try:
            async with connection(cfg) as conn:
                await cost.flush_to_db(conn, str(campaign_id))
        except Exception as e:
            print(f"[runner] WARNING: cost flush failed: {e!r}")
        # Cross-version regression detection (ARCHITECTURE §6): for every
        # FAIL in this campaign whose case_id last PASSed on a different
        # target_version, insert a cross_regressions row. Same failure
        # discipline as cost flush — log + continue, never mask the
        # campaign result.
        try:
            async with connection(cfg) as conn:
                n_inserted = await detect_for_campaign(conn, campaign_id)
            if n_inserted:
                print(f"[runner] cross-regression detection: {n_inserted} new event(s)")
        except Exception as e:
            print(f"[runner] WARNING: cross-regression detection failed: {e!r}")
        return campaign_id
    finally:
        # Flush Langfuse buffer first — events ship before any of the
        # later teardown steps can interfere. No-op when tracing is
        # disabled (no LANGFUSE_* env keys).
        try:
            observability.flush()
        except Exception as e:
            print(f"[runner] WARNING: langfuse flush failed: {e!r}")
        # Drain the asyncpg pool so the CLI exits cleanly instead of
        # blocking ~10s on keepalive tasks. Wrapped in try/except so a
        # pool-close failure can't mask the campaign's primary result.
        try:
            await close_pool()
        except Exception as e:
            print(f"[runner] WARNING: pool close failed: {e!r}")
