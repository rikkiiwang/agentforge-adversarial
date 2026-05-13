"""LangGraph state machine for one campaign run.

Replaces the straight-line `for entry in queue` loop in `runner.py` with a
5-node graph whose value is in the conditional edge from `judge` to
`class_probe` — on FAIL, fan out 10 boundary variants and loop back to
`dispatch` (bounded by `max_rounds`). Same DB invariants: two-phase write
into attack_runs, ensemble Judge UPDATE, atomic CHECK.

State shape (CampaignState):
  - inputs:  cfg, target_row, cases_path, mutate, max_rounds
  - mutable: campaign_id, pending (list[QueueEntry]), completed (list[AttackRun]),
             round_num, fail_count_this_round, openai_client (handle), chat_client
  - the actual DB connection is created per-node via the connection() ctx
    manager so a node can be retried without leaking connections.

Nodes:
  load_seeds   →  reads YAML cases dir, creates campaign row, enqueues seeds
  mutate       →  Red Team mutator (3 variants per seed), enqueues mutations
  dispatch     →  for each pending QueueEntry, call chat_client + insert run
  judge        →  ensemble Judge → UPDATE attack_runs
  class_probe  →  for each FAIL this round, generate 10 boundary variants
                  and enqueue them at round_num+1
  decide       →  conditional edge: if any FAIL this round AND
                  round_num < max_rounds, → class_probe → dispatch. Else END.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph
from openai import AsyncOpenAI

from agentforge_adversarial.cases import load_cases
from agentforge_adversarial.config import Config
from agentforge_adversarial.db import connection
from agentforge_adversarial.judges.ensemble import (
    judge_attack_run,
    update_attack_run_with_verdict,
)
from agentforge_adversarial.llm import MUTATOR_MODEL
from agentforge_adversarial.models import AttackRun, QueueEntry
from agentforge_adversarial.queue import create_campaign, enqueue_cases
from agentforge_adversarial.red_team.class_probe import (
    CLASS_PROBE_SUBAGENT_ID,
    generate_boundary_variants,
)
from agentforge_adversarial.red_team.mutator import (
    MUTATOR_SUBAGENT_ID,
    mutate_case,
)
from agentforge_adversarial.target import (
    ChatClient,
    dispatch_to_attack_run,
    insert_attack_run,
)


MAX_ROUNDS_DEFAULT = 2


def _accumulate(left: list, right: list) -> list:
    """Reducer: nodes return additive lists; the graph appends."""
    return (left or []) + (right or [])


class CampaignState(TypedDict, total=False):
    # Inputs (set by runner.py before graph.invoke).
    cfg: Config
    target_row: dict[str, Any]
    target_version: str
    cases_path: Path
    mutate: bool
    max_rounds: int
    openai_client: AsyncOpenAI | None
    chat_client: ChatClient

    # Mutable graph-internal state.
    campaign_id: UUID
    pending: list[QueueEntry]  # consumed by dispatch each round
    dispatched_this_round: list[AttackRun]  # judge reads this
    completed: Annotated[list[AttackRun], _accumulate]
    fails_this_round: list[AttackRun]  # class_probe reads this
    round_num: int


async def load_seeds_node(state: CampaignState) -> dict[str, Any]:
    cfg = state["cfg"]
    cases_path: Path = state["cases_path"]
    seeds = load_cases(cases_path)
    print(f"[graph:load_seeds] loaded {len(seeds)} seed cases from {cases_path}")
    async with connection(cfg) as conn:
        campaign_id = await create_campaign(
            conn,
            name=f"campaign-{cases_path.name}",
            target_version=state["target_version"],
            notes=f"target={state['target_row']['name']}",
            target_id=state["target_row"]["id"],
        )
        entries = await enqueue_cases(conn, campaign_id, seeds)
    print(f"[graph:load_seeds] campaign {campaign_id}, enqueued {len(entries)} seeds")
    print(f"[campaign] created {campaign_id} (target_version={state['target_version']})")
    return {
        "campaign_id": campaign_id,
        "pending": entries,
        "round_num": 0,
        "completed": [],
        "fails_this_round": [],
    }


async def mutate_node(state: CampaignState) -> dict[str, Any]:
    """Produce 3 LLM mutations per seed. Skipped if mutate=False or no LLM key."""
    if not state.get("mutate") or state.get("openai_client") is None:
        print("[graph:mutate] skipped (mutate=False or no OPENAI_API_KEY)")
        return {}
    cfg = state["cfg"]
    seeds_entries = state.get("pending") or []
    seed_cases = load_cases(state["cases_path"])
    by_case_id = {c.id: c for c in seed_cases}
    extras = []
    openai_client = state["openai_client"]
    for entry in seeds_entries:
        seed = by_case_id.get(entry.case_id)
        if seed is None:
            continue
        seed_mutations = await mutate_case(openai_client, seed)
        extras.extend(seed_mutations)
    print(f"[graph:mutate] produced {len(extras)} mutations across {len(seeds_entries)} seeds")
    if not extras:
        return {}
    async with connection(cfg) as conn:
        mut_entries = await enqueue_cases(
            conn,
            state["campaign_id"],
            extras,
            red_team_subagent_id=MUTATOR_SUBAGENT_ID,
            red_team_model=MUTATOR_MODEL,
        )
    return {"pending": mut_entries}


async def dispatch_node(state: CampaignState) -> dict[str, Any]:
    """Dispatch every QueueEntry in `pending` against the chat_client."""
    cfg = state["cfg"]
    chat_client = state["chat_client"]
    target_version = state["target_version"]
    pending = state.get("pending") or []
    print(f"[graph:dispatch] dispatching {len(pending)} attacks (round={state.get('round_num', 0)})")
    runs: list[AttackRun] = []
    async with connection(cfg) as conn:
        for entry in pending:
            run = await dispatch_to_attack_run(entry, chat_client, target_version)
            await insert_attack_run(conn, run)
            runs.append(run)
    # `pending` consumed — clear it for the next round.
    return {"pending": [], "completed": runs, "dispatched_this_round": runs}


async def judge_node(state: CampaignState) -> dict[str, Any]:
    """Ensemble Judge each run dispatched this round."""
    cfg = state["cfg"]
    openai_client = state.get("openai_client")
    dispatched: list[AttackRun] = state.get("dispatched_this_round") or []
    fails: list[AttackRun] = []
    async with connection(cfg) as conn:
        for run in dispatched:
            verdict = await judge_attack_run(run, openai_client=openai_client)
            await update_attack_run_with_verdict(conn, run.id, verdict)
            run.judge_verdict = verdict.verdict
            run.judge_reasoning = verdict.reasoning
            run.judge_rubric_version = verdict.rubric_version
            run.category_validated = verdict.category_validated
            print(
                f"[{run.case_id}] {verdict.verdict.upper():7} {verdict.rubric_version}: "
                f"{verdict.reasoning[:100]}"
            )
            if verdict.verdict == "fail":
                fails.append(run)
    print(f"[graph:judge] judged {len(dispatched)} runs; FAIL count this round = {len(fails)}")
    return {"fails_this_round": fails}


async def class_probe_node(state: CampaignState) -> dict[str, Any]:
    """For each FAIL this round, fan out 10 boundary variants. Increment round_num."""
    cfg = state["cfg"]
    openai_client = state.get("openai_client")
    fails = state.get("fails_this_round") or []
    round_num = state.get("round_num", 0)
    next_round = round_num + 1
    if openai_client is None or not fails:
        print(f"[graph:class_probe] no fan-out (no FAILs or no LLM); ending round {next_round}")
        return {"round_num": next_round, "fails_this_round": []}
    new_entries: list[QueueEntry] = []
    async with connection(cfg) as conn:
        for failing_run in fails:
            variants = await generate_boundary_variants(openai_client, failing_run)
            if not variants:
                continue
            entries = await enqueue_cases(
                conn,
                state["campaign_id"],
                variants,
                red_team_subagent_id=CLASS_PROBE_SUBAGENT_ID,
                red_team_model=MUTATOR_MODEL,
                parent_id=failing_run.id,
                round_num=next_round,
            )
            new_entries.extend(entries)
    print(
        f"[graph:class_probe] round {next_round}: fanned out "
        f"{len(fails)} FAILs into {len(new_entries)} boundary variants"
    )
    return {
        "round_num": next_round,
        "pending": new_entries,
        "fails_this_round": [],
    }


def decide_after_judge(state: CampaignState) -> str:
    """Conditional edge: fan out if any FAIL and round budget remains."""
    fails = state.get("fails_this_round") or []
    round_num = state.get("round_num", 0)
    max_rounds = state.get("max_rounds", MAX_ROUNDS_DEFAULT)
    if fails and round_num < max_rounds:
        return "class_probe"
    return END


def build_graph() -> Any:
    g = StateGraph(CampaignState)
    g.add_node("load_seeds", load_seeds_node)
    g.add_node("mutate", mutate_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("judge", judge_node)
    g.add_node("class_probe", class_probe_node)

    g.set_entry_point("load_seeds")
    g.add_edge("load_seeds", "mutate")
    g.add_edge("mutate", "dispatch")
    g.add_edge("dispatch", "judge")
    g.add_conditional_edges("judge", decide_after_judge, {"class_probe": "class_probe", END: END})
    g.add_edge("class_probe", "dispatch")

    return g.compile()
