"""Orchestrator — advisory cell-scoring loop.

ARCHITECTURE.md §2 / §4 step 2: compute the 5-signal weighted score per
``threat_model_cells`` row, pick the top cell, and emit a CampaignBrief
that records *why* the platform decided to run this campaign. Pure
Python — no LLM call in the decision path.

This is the **advisory** shape (the "(a) advisory orchestrator" decision
locked in tonight): the brief and the scoring breakdown are persisted
to ``campaigns.brief_json`` for auditability, but the actual attack
flow still dispatches all native seeds + synthesized mutator output.
The orchestrator's signal is visible without restricting the campaign
to one cell — best for demos where heat-map coverage matters.

The five signals (per ARCHITECTURE.md):

  - **coverage_gap** ∈ [0, 1] — fraction of "target_runs - current_runs"
    in the trailing window. 1.0 = totally untested cell.
  - **partial_rate** ∈ [0, 1] — share of PARTIAL verdicts in the cell's
    recent runs. High = boundary nearby, worth re-mutating.
  - **severity_baseline** ∈ [0, 1] — normalized severity tier from
    threat_model_cells.severity_baseline.
  - **staleness** ∈ [0, 1] — days since last judged attack on this cell,
    clipped to a 14-day window (older = no extra signal).
  - **diversity_score** ∈ [0, 1] — proxy: 1 - (n_unique_case_ids /
    n_total_runs) in the trailing window. Low = same case_id keeps
    running; cell needs fresh mutator output.

Composite (weights locked tonight):
    score = 0.30·coverage_gap + 0.30·partial_rate +
            0.20·severity_baseline + 0.10·staleness +
            0.10·diversity_score

Hard constraints from §4 step 2 ("any overdue regression_schedule row
forces an immediate replay; a detected target_version change triggers
a full sweep") are deliberately out of MVP scope — the slim regression
harness (CLI + Vuln-Board button) covers them by operator action. The
orchestrator records them as `null` in the brief so a future cron can
pick them up without schema migration.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

import asyncpg


# Window over which we compute coverage / staleness / diversity signals.
# Same value the dashboard's "recent campaigns" tile uses; 14 days is
# operationally meaningful (covers a typical demo + a follow-up sprint).
SIGNAL_WINDOW_DAYS = 14

# How many runs per cell qualify as "well-covered" — coverage_gap goes to
# 0 at TARGET_RUNS_PER_CELL. Tunable; this baseline came from "if every
# native YAML ran 4 times per window we'd consider that adequate."
TARGET_RUNS_PER_CELL = 8

# 5-signal weights (locked tonight). Sum to 1.0 so composite ∈ [0, 1].
WEIGHTS = {
    "coverage_gap":     0.30,
    "partial_rate":     0.30,
    "severity_baseline": 0.20,
    "staleness":        0.10,
    "diversity_score":  0.10,
}

_SEVERITY_NORM = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}


@dataclass
class CellScore:
    """One row in the orchestrator's scoring breakdown."""
    cell_id: str  # UUID stringified for JSON
    category: str
    subcategory: str
    channel: str
    severity_baseline: str
    runs_in_window: int
    partial_rate: float
    coverage_gap: float
    severity_signal: float
    staleness: float
    diversity_score: float
    composite_score: float


@dataclass
class CampaignBrief:
    """Audit record of why this campaign was run.

    Persisted to ``campaigns.brief_json``. Carries the chosen cell, the
    full scoring breakdown (every cell, not just the winner — reviewers
    want to see what was rejected), and the weights used so the math is
    re-derivable from the row alone.
    """
    chosen_cell_id: str
    chosen_category: str
    chosen_subcategory: str
    chosen_channel: str
    chosen_score: float
    weights: dict[str, float]
    cells: list[CellScore]
    signal_window_days: int = SIGNAL_WINDOW_DAYS

    def to_json(self) -> str:
        return json.dumps(
            {
                "chosen_cell_id": self.chosen_cell_id,
                "chosen_category": self.chosen_category,
                "chosen_subcategory": self.chosen_subcategory,
                "chosen_channel": self.chosen_channel,
                "chosen_score": self.chosen_score,
                "weights": self.weights,
                "cells": [asdict(c) for c in self.cells],
                "signal_window_days": self.signal_window_days,
            }
        )


def _composite(c: CellScore) -> float:
    return (
        WEIGHTS["coverage_gap"]      * c.coverage_gap
        + WEIGHTS["partial_rate"]    * c.partial_rate
        + WEIGHTS["severity_baseline"] * c.severity_signal
        + WEIGHTS["staleness"]       * c.staleness
        + WEIGHTS["diversity_score"] * c.diversity_score
    )


async def compute_cell_scores(
    conn: asyncpg.Connection,
    *,
    window_days: int = SIGNAL_WINDOW_DAYS,
) -> list[CellScore]:
    """Score every ``threat_model_cells`` row against current coverage.

    Single SQL pulls the cell catalog LEFT JOINed against an aggregate
    of ``attack_runs`` in the trailing window. Cells with no runs yet
    surface as coverage_gap=1.0 / partial_rate=0 / staleness=1.0 — the
    "totally untested" case dominates the composite score.
    """
    rows = await conn.fetch(
        f"""
        WITH window_runs AS (
            SELECT category, subcategory, channel,
                   judge_verdict, case_id, judged_at
              FROM attack_runs
             WHERE judged_at >= now() - INTERVAL '{int(window_days)} days'
        ),
        cell_stats AS (
            SELECT category,
                   subcategory,
                   channel,
                   COUNT(*)                                       AS runs_n,
                   COUNT(*) FILTER (WHERE judge_verdict = 'partial')::float
                       / NULLIF(COUNT(*), 0)                      AS partial_rate,
                   COUNT(DISTINCT case_id)::float
                       / NULLIF(COUNT(*), 0)                      AS unique_ratio,
                   MAX(judged_at)                                 AS last_judged_at
              FROM window_runs
             GROUP BY category, subcategory, channel
        )
        SELECT c.id::text       AS cell_id,
               c.category,
               c.subcategory,
               c.channel,
               c.severity_baseline,
               COALESCE(s.runs_n, 0)::int                          AS runs_n,
               COALESCE(s.partial_rate, 0.0)                       AS partial_rate,
               COALESCE(s.unique_ratio, 1.0)                       AS unique_ratio,
               s.last_judged_at                                    AS last_judged_at
          FROM threat_model_cells c
          LEFT JOIN cell_stats s
            ON s.category = c.category
           AND s.subcategory = c.subcategory
           AND s.channel = c.channel
         WHERE c.taxonomy_version = 'v1'
        """,
    )

    out: list[CellScore] = []
    now_ts = await conn.fetchval("SELECT now()")
    for r in rows:
        runs_n = int(r["runs_n"])
        coverage_gap = max(0.0, 1.0 - (runs_n / TARGET_RUNS_PER_CELL))
        severity_signal = _SEVERITY_NORM.get(r["severity_baseline"], 0.5)
        if r["last_judged_at"] is None:
            staleness = 1.0
        else:
            delta_days = (now_ts - r["last_judged_at"]).total_seconds() / 86400.0
            staleness = max(0.0, min(1.0, delta_days / float(window_days)))
        # Diversity: low when one case_id keeps running. 1 - unique_ratio
        # means "share of runs that were duplicates of an earlier case_id"
        # — a higher number prompts the orchestrator to send fresh attacks.
        diversity_score = 1.0 - float(r["unique_ratio"])

        score = CellScore(
            cell_id=r["cell_id"],
            category=r["category"],
            subcategory=r["subcategory"],
            channel=r["channel"],
            severity_baseline=r["severity_baseline"],
            runs_in_window=runs_n,
            partial_rate=float(r["partial_rate"]),
            coverage_gap=coverage_gap,
            severity_signal=severity_signal,
            staleness=staleness,
            diversity_score=diversity_score,
            composite_score=0.0,  # filled below
        )
        score.composite_score = _composite(score)
        out.append(score)
    return out


def select_top_cell(scores: list[CellScore]) -> CellScore | None:
    """Pick the highest composite score; tie-break by severity then alpha."""
    if not scores:
        return None
    return max(
        scores,
        key=lambda c: (
            c.composite_score,
            _SEVERITY_NORM.get(c.severity_baseline, 0.0),
            c.category,
            c.subcategory,
        ),
    )


def build_brief(scores: list[CellScore]) -> CampaignBrief | None:
    """Compose a CampaignBrief from the full scoring breakdown."""
    top = select_top_cell(scores)
    if top is None:
        return None
    return CampaignBrief(
        chosen_cell_id=top.cell_id,
        chosen_category=top.category,
        chosen_subcategory=top.subcategory,
        chosen_channel=top.channel,
        chosen_score=top.composite_score,
        weights=dict(WEIGHTS),
        cells=scores,
    )


async def persist_brief(
    conn: asyncpg.Connection,
    campaign_id: UUID,
    brief: CampaignBrief,
) -> None:
    """Stash the brief on the campaigns row so reviewers can answer
    "why did the platform run this?" from one row."""
    await conn.execute(
        "UPDATE campaigns SET brief_json = $1::jsonb WHERE id = $2",
        brief.to_json(),
        campaign_id,
    )


async def emit_brief_for_campaign(
    conn: asyncpg.Connection,
    campaign_id: UUID,
) -> CampaignBrief | None:
    """One-call entry point used by the graph at campaign start.

    Computes scores, picks the top cell, persists the brief. Returns
    the brief so the caller can print a banner line for the operator.
    """
    scores = await compute_cell_scores(conn)
    brief = build_brief(scores)
    if brief is not None:
        await persist_brief(conn, campaign_id, brief)
    return brief
