"""Unit tests for the per-campaign cost rollup buffer.

The DB flush path is exercised by integration tests; this module covers the
pure-logic in-memory accumulator + price calculation.
"""
from __future__ import annotations

from agentforge_adversarial import cost


class _FakeUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _FakeResp:
    def __init__(self, prompt: int, completion: int) -> None:
        self.usage = _FakeUsage(prompt, completion)


def setup_function(_fn) -> None:
    cost.reset_for_test()


def test_record_accumulates_tokens_and_cost() -> None:
    cost.set_campaign("camp-A")
    cost.record(_FakeResp(1_000, 500), "gpt-4o-mini")
    cost.record(_FakeResp(2_000, 1_000), "gpt-4o-mini")

    snap = cost.snapshot("camp-A")
    assert snap["tokens_in"] == 3_000
    assert snap["tokens_out"] == 1_500
    assert snap["calls"] == 2
    # gpt-4o-mini: $0.15 / 1M input, $0.60 / 1M output
    # 3000 * 0.15 / 1M + 1500 * 0.60 / 1M = 0.00045 + 0.00090 = 0.00135
    assert abs(snap["cost_usd"] - 0.00135) < 1e-9


def test_record_uses_per_model_pricing() -> None:
    """gpt-4o (full) should cost ~16× more than gpt-4o-mini for same tokens."""
    cost.set_campaign("camp-A")
    cost.record(_FakeResp(1_000, 1_000), "gpt-4o-mini")
    mini = cost.snapshot("camp-A")["cost_usd"]

    cost.reset_for_test()
    cost.set_campaign("camp-B")
    cost.record(_FakeResp(1_000, 1_000), "gpt-4o")
    full = cost.snapshot("camp-B")["cost_usd"]

    # Pricing snapshot 2026-05:
    #   mini: 0.15 + 0.60  = 0.75 / 1M
    #   full: 2.50 + 10.00 = 12.50 / 1M
    # Ratio = 12.50 / 0.75 ≈ 16.67
    assert full / mini > 15


def test_unknown_model_records_tokens_but_zero_cost() -> None:
    cost.set_campaign("camp-A")
    cost.record(_FakeResp(500, 200), "claude-sonnet-4-6")

    snap = cost.snapshot("camp-A")
    assert snap["tokens_in"] == 500
    assert snap["tokens_out"] == 200
    assert snap["calls"] == 1
    assert snap["cost_usd"] == 0.0


def test_record_without_set_campaign_is_noop() -> None:
    # No active campaign → no buffer mutation, no crash.
    cost.record(_FakeResp(1000, 1000), "gpt-4o-mini")
    # snapshot of nothing should give zeros, not raise:
    assert cost.snapshot()["cost_usd"] == 0.0


def test_record_handles_response_without_usage() -> None:
    """Mocked clients in tests typically return responses with no `usage`
    attribute. The recorder must no-op rather than crash."""
    cost.set_campaign("camp-A")

    class _NoUsage:
        usage = None

    cost.record(_NoUsage(), "gpt-4o-mini")
    snap = cost.snapshot("camp-A")
    assert snap["calls"] == 0


def test_campaigns_isolated_in_buffer() -> None:
    cost.set_campaign("camp-A")
    cost.record(_FakeResp(1_000, 500), "gpt-4o-mini")
    a_before = cost.snapshot("camp-A")

    cost.set_campaign("camp-B")
    cost.record(_FakeResp(99, 99), "gpt-4o")

    # camp-A's totals must not have changed.
    a_after = cost.snapshot("camp-A")
    assert a_after == a_before
