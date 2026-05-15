"""Unit tests for the advisory Orchestrator.

The DB-level SQL aggregation is integration-territory — covered when
``test_runner.py`` runs against a populated Postgres. Here we test the
pure-Python pieces that decide *what* to pick once cell statistics are
known: weight composition, tie-breaking, brief serialization.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from agentforge_adversarial.orchestrator import (
    WEIGHTS,
    CellScore,
    build_brief,
    select_top_cell,
)


def _cell(
    *,
    category: str = "prompt_injection",
    subcategory: str = "persona_hijack",
    channel: str = "direct",
    severity: str = "high",
    coverage_gap: float = 0.5,
    partial_rate: float = 0.0,
    staleness: float = 0.5,
    diversity_score: float = 0.0,
) -> CellScore:
    from agentforge_adversarial.orchestrator import _SEVERITY_NORM
    score = CellScore(
        cell_id=str(uuid4()),
        category=category,
        subcategory=subcategory,
        channel=channel,
        severity_baseline=severity,
        runs_in_window=0,
        partial_rate=partial_rate,
        coverage_gap=coverage_gap,
        severity_signal=_SEVERITY_NORM[severity],
        staleness=staleness,
        diversity_score=diversity_score,
        composite_score=0.0,
    )
    # Composite is computed by compute_cell_scores in production; replicate
    # the same formula here so the test cells carry a meaningful score.
    score.composite_score = (
        WEIGHTS["coverage_gap"]      * score.coverage_gap
        + WEIGHTS["partial_rate"]    * score.partial_rate
        + WEIGHTS["severity_baseline"] * score.severity_signal
        + WEIGHTS["staleness"]       * score.staleness
        + WEIGHTS["diversity_score"] * score.diversity_score
    )
    return score


def test_weights_sum_to_one() -> None:
    """Composite score must be bounded in [0, 1]; the weights have to
    sum to 1.0 for the composite to be interpretable as a percentage."""
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_select_top_cell_picks_max_composite() -> None:
    """Untested critical cell should beat a well-covered medium cell."""
    untested_critical = _cell(severity="critical", coverage_gap=1.0, staleness=1.0)
    well_covered_medium = _cell(severity="medium", coverage_gap=0.1, staleness=0.1)
    top = select_top_cell([well_covered_medium, untested_critical])
    assert top is untested_critical


def test_select_top_cell_partial_rate_dominates_when_coverage_equal() -> None:
    """Two cells with identical coverage / staleness — the one with a
    higher PARTIAL rate (boundary nearby) should win because the
    partial_rate weight is high (0.30, tied with coverage_gap)."""
    cold = _cell(coverage_gap=0.3, partial_rate=0.0)
    warm = _cell(coverage_gap=0.3, partial_rate=0.8)
    top = select_top_cell([cold, warm])
    assert top is warm


def test_select_top_cell_returns_none_for_empty_list() -> None:
    assert select_top_cell([]) is None


def test_severity_breaks_score_tie() -> None:
    """If composite scores tie, the tie-break order is severity then
    category alpha. Construct two cells with identical signals but
    different severities to verify."""
    a = _cell(severity="high", coverage_gap=0.5, staleness=0.5)
    b = _cell(severity="critical", coverage_gap=0.5, staleness=0.5)
    # Force composite equality by overwriting the score (the helper
    # otherwise gives critical a higher composite via severity_signal).
    a.composite_score = b.composite_score = 0.5
    top = select_top_cell([a, b])
    assert top is b  # critical wins the tie


def test_build_brief_carries_full_scoring_breakdown() -> None:
    """Reviewers want to see *all* cells, not just the winner, so they
    can audit why the orchestrator passed on others.

    Fixtures chosen so severity is the deciding factor: all three cells
    have coverage_gap=1.0 and equal staleness/partial_rate, so the
    composite reduces to ``0.30·1 + 0.10·1 + 0.20·severity_norm`` →
    critical wins.
    """
    cells = [
        _cell(category="prompt_injection", coverage_gap=1.0, staleness=1.0),
        _cell(category="dos_cost", coverage_gap=1.0, staleness=1.0, severity="medium"),
        _cell(category="tool_misuse", coverage_gap=1.0, staleness=1.0, severity="critical"),
    ]
    brief = build_brief(cells)
    assert brief is not None
    # Winner: tool_misuse (critical edges out high when coverage_gap is tied).
    assert brief.chosen_category == "tool_misuse"
    # All cells preserved in the breakdown.
    assert len(brief.cells) == 3
    assert {c.category for c in brief.cells} == {"prompt_injection", "dos_cost", "tool_misuse"}


def test_brief_serializes_to_valid_json() -> None:
    """Brief lands on campaigns.brief_json (JSONB). Round-trip safety."""
    brief = build_brief([_cell()])
    assert brief is not None
    payload = json.loads(brief.to_json())
    assert payload["chosen_category"] == "prompt_injection"
    assert payload["weights"] == WEIGHTS
    assert len(payload["cells"]) == 1
    # Each cell dict carries the audit fields the dashboard tile needs.
    cell0 = payload["cells"][0]
    assert "coverage_gap" in cell0
    assert "partial_rate" in cell0
    assert "composite_score" in cell0
    assert "runs_in_window" in cell0


def test_build_brief_returns_none_for_empty_scores() -> None:
    assert build_brief([]) is None
