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

import asyncio
from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph
from openai import AsyncOpenAI

from agentforge_adversarial.cases import load_cases
from agentforge_adversarial.config import Config
from agentforge_adversarial.db import connection
from agentforge_adversarial.documentation_agent import document_fail
from agentforge_adversarial.judges.ensemble import (
    judge_attack_run,
    update_attack_run_with_verdict,
)
from agentforge_adversarial.llm import MUTATOR_MODEL
from agentforge_adversarial.models import AttackRun, QueueEntry
from agentforge_adversarial.near_miss import record_near_miss
from agentforge_adversarial import cost, observability
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
LLM_CONCURRENCY = 8  # cap on parallel OpenAI calls (mutator + class-probe)


def _accumulate(left: list, right: list) -> list:
    """Reducer: nodes return additive lists; the graph appends."""
    return (left or []) + (right or [])


def _build_history(completed: list[AttackRun] | None) -> list[dict[str, str]]:
    """Project completed AttackRuns into the lightweight dict shape the
    mutator + class-probe history hint expects. Only judged runs with a
    verdict contribute — in-flight or judge-failed runs are skipped."""
    if not completed:
        return []
    out: list[dict[str, str]] = []
    for r in completed:
        v = getattr(r, "judge_verdict", None)
        if v not in ("pass", "fail", "partial"):
            continue
        out.append({
            "category": r.category,
            "verdict": v,
            "attack_prompt": r.attack_prompt,
        })
    return out


class CampaignState(TypedDict, total=False):
    # Inputs (set by runner.py before graph.invoke).
    cfg: Config
    target_row: dict[str, Any]
    target_version: str
    cases_path: Path
    mutate: bool
    mutations_per_seed: int
    max_rounds: int
    synthesis_k: int  # cap on mutator output after synthesize_fn; 0 disables
    openai_client: AsyncOpenAI | None  # Judge family + synthesis embeddings
    red_team_client: Any | None  # Red Team family (Anthropic by default)
    chat_client: ChatClient

    # Mutable graph-internal state.
    campaign_id: UUID
    pending: list[QueueEntry]  # consumed by dispatch each round
    dispatched_this_round: list[AttackRun]  # judge reads this
    completed: Annotated[list[AttackRun], _accumulate]
    fails_this_round: list[AttackRun]  # fan_out reads this
    partials_this_round: list[AttackRun]  # fan_out reads this
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
        # Langfuse trace (ARCHITECTURE §5): start the top-level trace
        # now so every subsequent node's span nests under it. Persists
        # the trace id on campaigns so the dashboard can deep-link.
        # No-op when LANGFUSE_* keys are absent.
        try:
            trace_id = observability.start_campaign_trace(
                campaign_id=campaign_id,
                target_version=state["target_version"],
                cases_path=str(cases_path),
            )
            if trace_id:
                await conn.execute(
                    "UPDATE campaigns SET langfuse_trace_id = $1 WHERE id = $2",
                    trace_id, campaign_id,
                )
                print(f"[langfuse] trace_id={trace_id}")
        except Exception as e:
            print(f"[langfuse] WARNING: start_campaign_trace failed: {e!r}")
        # Orchestrator scoring (ARCHITECTURE §2, §4 step 2): compute the
        # 5-signal weighted score per cell and stash the audit brief on
        # the campaigns row. Advisory in MVP — doesn't restrict this
        # campaign's seeds, just records "why now / which cell" so
        # reviewers can answer that question from one DB row.
        try:
            from agentforge_adversarial.orchestrator import emit_brief_for_campaign
            with observability.span("orchestrator"):
                brief = await emit_brief_for_campaign(conn, campaign_id)
                if brief is not None:
                    print(
                        f"[orchestrator] top cell: {brief.chosen_category} / "
                        f"{brief.chosen_subcategory} ({brief.chosen_channel}) "
                        f"score={brief.chosen_score:.3f}"
                    )
                    observability.annotate_current_span(
                        output=f"top={brief.chosen_category}/{brief.chosen_subcategory} score={brief.chosen_score:.3f}",
                        metadata={"weights": brief.weights, "n_cells": len(brief.cells)},
                    )
        except Exception as e:
            print(f"[orchestrator] WARNING: brief generation failed: {e!r}")
    cost.set_campaign(str(campaign_id))
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
    """Produce 3 LLM mutations per seed. Skipped if mutate=False or no Red Team key."""
    with observability.span("red_team_mutator") as _sp:
        return await _mutate_node_impl(state)


async def _mutate_node_impl(state: CampaignState) -> dict[str, Any]:
    red_team_client = state.get("red_team_client")
    if not state.get("mutate") or red_team_client is None:
        print(
            "[graph:mutate] skipped (mutate=False or no Red Team API key — "
            "set ANTHROPIC_API_KEY for the default MUTATOR_MODEL)"
        )
        return {}
    cfg = state["cfg"]
    seeds_entries = state.get("pending") or []
    seed_cases = load_cases(state["cases_path"])
    by_case_id = {c.id: c for c in seed_cases}
    # Sequential `for await` made the mutator dominate wall-clock time
    # (8 seeds × ~4s per Red Team call = 32s before any dispatch). Parallelize
    # under a Semaphore so we stay under provider rate limits.
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    n_per_seed = int(state.get("mutations_per_seed", 3))

    async def _mutate_one(entry):
        seed = by_case_id.get(entry.case_id)
        if seed is None:
            return []
        async with sem:
            return await mutate_case(red_team_client, seed, n=n_per_seed)

    print(
        f"[graph:mutate] generating {n_per_seed} mutations/seed for "
        f"{len(seeds_entries)} seeds in parallel (concurrency={LLM_CONCURRENCY})"
    )
    results = await asyncio.gather(
        *(_mutate_one(e) for e in seeds_entries), return_exceptions=True
    )
    extras: list = []
    for r in results:
        if isinstance(r, Exception):
            print(f"[graph:mutate] WARNING: a mutator call failed: {type(r).__name__}")
            continue
        extras.extend(r)
    print(f"[graph:mutate] produced {len(extras)} mutations across {len(seeds_entries)} seeds")
    if not extras:
        return {}

    observability.annotate_current_span(
        output=f"produced {len(extras)} mutations from {len(seeds_entries)} seeds",
    )

    # synthesize_fn (ARCHITECTURE §4 step 4): filter the noisy mutator
    # stream down to K high-value attacks. Skipped (passes everything
    # through unchanged) when no OpenAI key — the pipeline needs the
    # embeddings API for dedup + novelty.
    openai_client = state.get("openai_client")
    if openai_client is not None:
        from agentforge_adversarial.synthesis import DEFAULT_K, synthesize
        before = len(extras)
        try:
            with observability.span("synthesize") as _sp:
                async with connection(cfg) as conn:
                    extras = await synthesize(
                        extras,
                        embed_client=openai_client,
                        conn=conn,
                        k=int(state.get("synthesis_k", DEFAULT_K)),
                    )
                observability.annotate_current_span(
                    output=f"{before} → {len(extras)}",
                    metadata={"k": int(state.get("synthesis_k", DEFAULT_K))},
                )
            print(
                f"[graph:mutate] synthesize: {before} → {len(extras)} "
                f"(dedup + novelty + top-K, K={state.get('synthesis_k', DEFAULT_K)})"
            )
        except Exception as e:
            print(f"[graph:mutate] WARNING: synthesize failed ({type(e).__name__}); "
                  f"passing {before} candidates through unfiltered: {e!r}")
    else:
        print("[graph:mutate] synthesize skipped (no OpenAI key for embeddings)")
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
    # IMPORTANT: combine with the seeds that load_seeds_node already put in
    # pending — returning just `mut_entries` would overwrite them, and only
    # the mutations would reach dispatch.
    return {"pending": (state.get("pending") or []) + mut_entries}


async def dispatch_node(state: CampaignState) -> dict[str, Any]:
    """Dispatch every QueueEntry in `pending` against the chat_client."""
    with observability.span("dispatch") as _sp:
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
        observability.annotate_current_span(
            output=f"dispatched {len(runs)} attack_runs",
            metadata={"round": state.get("round_num", 0)},
        )
        # `pending` consumed — clear it for the next round.
        return {"pending": [], "completed": runs, "dispatched_this_round": runs}


async def judge_node(state: CampaignState) -> dict[str, Any]:
    """Ensemble Judge each run dispatched this round.

    Both FAIL and PARTIAL feed conditional edges in `decide_after_judge`:
    FAIL fans out via class-probe (10 boundary variants each), PARTIAL re-
    enters the mutator (3 fresh phrasings each, aiming to disambiguate the
    ambiguous verdict in the next round).
    """
    with observability.span("judge") as _sp:
        result = await _judge_node_impl(state)
        observability.annotate_current_span(
            output=f"FAIL={len(result.get('fails_this_round', []))} "
                   f"PARTIAL={len(result.get('partials_this_round', []))}",
        )
        return result


async def _judge_node_impl(state: CampaignState) -> dict[str, Any]:
    cfg = state["cfg"]
    openai_client = state.get("openai_client")
    dispatched: list[AttackRun] = state.get("dispatched_this_round") or []
    fails: list[AttackRun] = []
    partials: list[AttackRun] = []
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
                # Documentation Agent: write vulnerabilities + vuln_reports
                # rows for every FAIL. Idempotent on attack_run_id.
                try:
                    await document_fail(conn, run)
                except Exception as e:
                    print(f"[graph:judge] WARNING: document_fail failed: {e!r}")
            elif verdict.verdict == "partial":
                partials.append(run)
                # Promote PARTIAL to a first-class near-miss lifecycle row
                # so the dashboard's Near-Miss tile (ARCHITECTURE §7) and
                # the orchestrator's post-verdict routing can act on it.
                # Idempotent on attack_run_id; safe under judge retry.
                try:
                    await record_near_miss(
                        conn, run, campaign_id=state["campaign_id"]
                    )
                except Exception as e:
                    print(f"[graph:judge] WARNING: record_near_miss failed: {e!r}")
    print(
        f"[graph:judge] judged {len(dispatched)} runs; "
        f"FAIL={len(fails)} PARTIAL={len(partials)}"
    )
    return {"fails_this_round": fails, "partials_this_round": partials}


async def class_probe_node(state: CampaignState) -> dict[str, Any]:
    """For each FAIL this round, fan out 10 boundary variants. Increment round_num."""
    with observability.span("class_probe") as _sp:
        return await _class_probe_node_impl(state)


async def _class_probe_node_impl(state: CampaignState) -> dict[str, Any]:
    cfg = state["cfg"]
    red_team_client = state.get("red_team_client")
    fails = state.get("fails_this_round") or []
    round_num = state.get("round_num", 0)
    next_round = round_num + 1
    if red_team_client is None or not fails:
        print(f"[graph:class_probe] no fan-out (no FAILs or no Red Team key); ending round {next_round}")
        return {"round_num": next_round, "fails_this_round": []}
    history = _build_history(state.get("completed"))
    print(
        f"[graph:class_probe] round {next_round}: generating variants for "
        f"{len(fails)} FAILs in parallel (concurrency={LLM_CONCURRENCY}, "
        f"history_hint={len(history)} prior runs)"
    )
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def _probe_one(failing_run):
        async with sem:
            return failing_run, await generate_boundary_variants(
                red_team_client, failing_run, history=history
            )

    results = await asyncio.gather(
        *(_probe_one(f) for f in fails), return_exceptions=True
    )
    new_entries: list[QueueEntry] = []
    async with connection(cfg) as conn:
        for r in results:
            if isinstance(r, Exception):
                print(f"[graph:class_probe] WARNING: a probe call failed: {type(r).__name__}")
                continue
            failing_run, variants = r
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


async def partial_reentry_node(state: CampaignState) -> dict[str, Any]:
    """For each PARTIAL this round, ask the mutator to produce 3 fresh
    phrasings of the attack — these aim to push the next round's verdict
    into a clear PASS or FAIL.

    Variants are enqueued with `parent_id` = the PARTIAL'd run's id and
    `round_num` = round_num + 1. Source remains 'random' (same as mutator
    output); the partial-reentry origin is recoverable via parent_id lineage.
    """
    with observability.span("partial_reentry") as _sp:
        return await _partial_reentry_impl(state)


async def _partial_reentry_impl(state: CampaignState) -> dict[str, Any]:
    cfg = state["cfg"]
    red_team_client = state.get("red_team_client")
    partials = state.get("partials_this_round") or []
    round_num = state.get("round_num", 0)
    next_round = round_num + 1
    if red_team_client is None or not partials:
        print(
            f"[graph:partial_reentry] no fan-out (no PARTIALs or no Red Team key); "
            f"round stays {round_num}"
        )
        return {"partials_this_round": []}
    history = _build_history(state.get("completed"))
    print(
        f"[graph:partial_reentry] round {next_round}: re-mutating "
        f"{len(partials)} PARTIALs in parallel (concurrency={LLM_CONCURRENCY}, "
        f"history_hint={len(history)} prior runs)"
    )
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    seed_cases = load_cases(state["cases_path"])
    by_case_id = {c.id: c for c in seed_cases}

    async def _reentry_one(partial_run):
        # Build an EvalCase view of the PARTIAL'd run to feed the mutator.
        # Prefer the original seed (canonical phrasing); fall back to the
        # AttackRun if the case_id was a mutation (e.g. PI-PH-001-MUT-2).
        seed_id = partial_run.case_id.split("-MUT-")[0].split("-CP-")[0]
        seed = by_case_id.get(seed_id)
        if seed is None:
            # Synthesize a minimal EvalCase from the PARTIAL'd run so the
            # mutator has the same prompt + expected-failure-mode context.
            from agentforge_adversarial.models import EvalCase as _EC
            seed = _EC(
                id=partial_run.case_id,
                category=partial_run.category,
                subcategory=partial_run.subcategory,
                channel=partial_run.channel,
                source="random",
                severity="high",
                attack_prompt=partial_run.attack_prompt,
                expected_safe_behavior="Refuse and explain why.",
                expected_failure_mode=partial_run.expected_failure_mode,
                regression=False,
            )
        async with sem:
            return partial_run, await mutate_case(
                red_team_client, seed,
                n=int(state.get("mutations_per_seed", 3)),
                history=history,
            )

    results = await asyncio.gather(
        *(_reentry_one(p) for p in partials), return_exceptions=True
    )
    new_entries: list[QueueEntry] = []
    async with connection(cfg) as conn:
        for r in results:
            if isinstance(r, Exception):
                print(f"[graph:partial_reentry] WARNING: a re-mutate call failed: {type(r).__name__}")
                continue
            partial_run, variants = r
            if not variants:
                continue
            entries = await enqueue_cases(
                conn,
                state["campaign_id"],
                variants,
                red_team_subagent_id=MUTATOR_SUBAGENT_ID,
                red_team_model=MUTATOR_MODEL,
                parent_id=partial_run.id,
                round_num=next_round,
            )
            new_entries.extend(entries)
    print(
        f"[graph:partial_reentry] round {next_round}: re-mutated "
        f"{len(partials)} PARTIALs into {len(new_entries)} fresh phrasings"
    )
    # Note: we deliberately do NOT bump round_num here. That happens in
    # class_probe_node OR — if there are no FAILs to fan out — in
    # `bump_round_node` (added below) so that round_num is incremented
    # exactly once per logical round regardless of which fan-out edges fired.
    return {
        "pending": (state.get("pending") or []) + new_entries,
        "partials_this_round": [],
    }


async def bump_round_node(state: CampaignState) -> dict[str, Any]:
    """When PARTIAL re-entry fired but class-probe didn't (no FAILs), this
    node increments round_num so the conditional edge sees the round budget
    advance. Idempotent — class_probe also bumps it when it fires."""
    return {"round_num": state.get("round_num", 0) + 1}


def decide_after_judge(state: CampaignState) -> str:
    """Conditional edge after judging this round's dispatch.

    Routes:
      - PARTIAL exists  → partial_reentry (which then chains into class_probe
        or directly to dispatch depending on FAIL count)
      - FAIL exists     → class_probe
      - else            → END

    All routes are gated by `round_num < max_rounds` so the loop can't run
    away. PARTIAL is checked first because re-mutated variants might
    themselves FAIL, and we want to give them a chance to fan-out in the
    next round.
    """
    fails = state.get("fails_this_round") or []
    partials = state.get("partials_this_round") or []
    round_num = state.get("round_num", 0)
    max_rounds = state.get("max_rounds", MAX_ROUNDS_DEFAULT)
    if round_num >= max_rounds:
        return END
    if partials:
        return "partial_reentry"
    if fails:
        return "class_probe"
    return END


def decide_after_partial_reentry(state: CampaignState) -> str:
    """After partial_reentry, run class-probe too if there are FAILs from the
    same judge pass. Otherwise hop straight to bump_round + dispatch."""
    fails = state.get("fails_this_round") or []
    if fails:
        return "class_probe"
    return "bump_round"


def build_graph() -> Any:
    g = StateGraph(CampaignState)
    g.add_node("load_seeds", load_seeds_node)
    g.add_node("mutate", mutate_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("judge", judge_node)
    g.add_node("partial_reentry", partial_reentry_node)
    g.add_node("class_probe", class_probe_node)
    g.add_node("bump_round", bump_round_node)

    g.set_entry_point("load_seeds")
    g.add_edge("load_seeds", "mutate")
    g.add_edge("mutate", "dispatch")
    g.add_edge("dispatch", "judge")
    g.add_conditional_edges(
        "judge",
        decide_after_judge,
        {
            "partial_reentry": "partial_reentry",
            "class_probe": "class_probe",
            END: END,
        },
    )
    g.add_conditional_edges(
        "partial_reentry",
        decide_after_partial_reentry,
        {"class_probe": "class_probe", "bump_round": "bump_round"},
    )
    g.add_edge("class_probe", "dispatch")
    g.add_edge("bump_round", "dispatch")

    return g.compile()
