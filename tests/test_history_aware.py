"""Unit tests for history-aware mutator + class-probe prompts.

The mutator + class-probe in rounds 1+ get a history hint listing which
prompts the target already REFUSED (= PASS) and which SUCCEEDED (= FAIL).
This biases LLM-generated variants away from already-defended framings.

These tests exercise the pure formatting helper. The full integration is
covered by the live graph runs in test_runner.py / e2e campaigns.
"""
from __future__ import annotations

from agentforge_adversarial.red_team.mutator import (
    HISTORY_HINT_TEMPLATE,
    _format_history_hint,
)


def test_returns_empty_string_when_no_history() -> None:
    assert _format_history_hint(None, "prompt_injection") == ""
    assert _format_history_hint([], "prompt_injection") == ""


def test_filters_to_same_category() -> None:
    """Cross-category history is noise — only same-category runs steer the
    mutator. Otherwise the hint becomes garbage on diverse campaigns."""
    history = [
        {"category": "prompt_injection", "verdict": "pass",
         "attack_prompt": "PI defended prompt"},
        {"category": "tool_misuse", "verdict": "fail",
         "attack_prompt": "TM unrelated success"},
    ]
    hint = _format_history_hint(history, "prompt_injection")
    assert "PI defended prompt" in hint
    assert "TM unrelated success" not in hint


def test_separates_pass_and_fail_into_distinct_buckets() -> None:
    """The two buckets carry opposite guidance: avoid PASSed, extend FAILed."""
    history = [
        {"category": "prompt_injection", "verdict": "pass",
         "attack_prompt": "refused phrasing X"},
        {"category": "prompt_injection", "verdict": "fail",
         "attack_prompt": "successful phrasing Y"},
    ]
    hint = _format_history_hint(history, "prompt_injection")
    # Split on the SUCCEEDED section header to isolate the two buckets.
    # The header appears as 'SUCCEEDED (the assistant complied'.
    refused_block, _, succeeded_block = hint.partition("SUCCEEDED (the assistant")
    assert "refused phrasing X" in refused_block
    assert "refused phrasing X" not in succeeded_block
    assert "successful phrasing Y" in succeeded_block
    assert "successful phrasing Y" not in refused_block


def test_caps_bucket_size_at_five() -> None:
    """A long campaign would otherwise blow out the context window."""
    history = [
        {"category": "prompt_injection", "verdict": "pass",
         "attack_prompt": f"refused {i}"}
        for i in range(20)
    ]
    hint = _format_history_hint(history, "prompt_injection")
    assert "refused 0" in hint and "refused 4" in hint
    assert "refused 5" not in hint  # capped at 5


def test_truncates_prompt_to_120_chars() -> None:
    """Long seed prompts must not balloon the system prompt — truncate each
    history line to 120 chars."""
    long_prompt = "x" * 500
    history = [
        {"category": "prompt_injection", "verdict": "pass",
         "attack_prompt": long_prompt},
    ]
    hint = _format_history_hint(history, "prompt_injection")
    # Find the single hint line; it should have at most 120 'x'.
    x_runs = [seg for seg in hint.split() if set(seg) == {"x"}]
    assert x_runs and len(x_runs[0]) == 120


def test_partial_verdicts_excluded() -> None:
    """PARTIAL is ambiguous — not useful steering signal for either bucket."""
    history = [
        {"category": "prompt_injection", "verdict": "partial",
         "attack_prompt": "ambiguous prompt"},
    ]
    hint = _format_history_hint(history, "prompt_injection")
    # No PASS and no FAIL → returns empty string per the contract.
    assert hint == ""


def test_template_has_both_steering_directions() -> None:
    """The template itself must instruct both 'avoid REFUSED' and 'extend
    SUCCEEDED' — otherwise the hint becomes one-sided."""
    assert "Do NOT regenerate" in HISTORY_HINT_TEMPLATE
    assert "DO extend" in HISTORY_HINT_TEMPLATE
