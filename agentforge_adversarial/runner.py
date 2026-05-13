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

from openai import AsyncOpenAI

from agentforge_adversarial.config import Config
from agentforge_adversarial.db import connection
from agentforge_adversarial.graph import MAX_ROUNDS_DEFAULT, build_graph
from agentforge_adversarial.target import ChatClient, make_client
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
    max_rounds: int = MAX_ROUNDS_DEFAULT,
) -> UUID:
    openai_client = (
        AsyncOpenAI(api_key=cfg.openai_api_key) if cfg.openai_api_key else None
    )

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
        "max_rounds": max_rounds,
        "openai_client": openai_client,
        "chat_client": chat_client,
    }
    final_state = await graph.ainvoke(initial)
    return final_state["campaign_id"]
