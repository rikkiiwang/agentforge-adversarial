"""Per-campaign LLM cost rollup.

A ContextVar carries the active campaign_id through the graph; each LLM
call site calls `record(resp, model)` after `chat.completions.create(...)`.
Tokens and dollar cost accumulate in a process-local buffer keyed by
campaign_id. `flush_to_db` writes the totals into `campaigns` at the end
of the graph run.

Rationale for the ContextVar pattern: the alternative is threading a
campaign_id parameter through `mutate_case`, `judge_llm`, `class_probe`,
`partial_reentry` — four functions all called inside `asyncio.gather`.
ContextVars propagate across asyncio tasks automatically, so set-once at
graph start and read-on-each-record works without signature churn.

Prices are a 2026-05 snapshot of OpenAI's public per-1M-token rates for
the models the harness uses. Unknown models → cost 0 (still counts tokens).
"""
from __future__ import annotations

import contextvars
from typing import Any

# Per-1M-token USD prices (OpenAI public list, 2026-05 snapshot).
_PRICE_PER_1M: dict[str, dict[str, float]] = {
    "gpt-4o-mini":  {"in": 0.15, "out": 0.60},
    "gpt-4o":       {"in": 2.50, "out": 10.00},
    "gpt-4.1-mini": {"in": 0.40, "out": 1.60},
}

_current_campaign_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agentforge_campaign_id", default=None
)

# Process-local buffer. Keyed by campaign_id so concurrent campaigns in
# the same process (a future feature) don't cross-pollute. In practice the
# harness runs one campaign per process; this is defensive.
_buffer: dict[str, dict[str, float]] = {}


def set_campaign(campaign_id: str) -> None:
    """Mark a campaign as active for the current asyncio context."""
    _current_campaign_id.set(campaign_id)
    _buffer.setdefault(
        campaign_id,
        {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0},
    )


def record(resp: Any, model: str) -> None:
    """Capture token usage + computed cost from an OpenAI chat completion.

    Safe to call from any task — no-op when no campaign is set or the
    response has no usage data (e.g. a mocked client in tests).
    """
    cid = _current_campaign_id.get()
    if cid is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    pin = int(getattr(usage, "prompt_tokens", 0) or 0)
    pout = int(getattr(usage, "completion_tokens", 0) or 0)
    rate = _PRICE_PER_1M.get(model, {"in": 0.0, "out": 0.0})
    cost = (pin * rate["in"] + pout * rate["out"]) / 1_000_000.0
    b = _buffer.setdefault(
        cid, {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0}
    )
    b["cost_usd"] += cost
    b["tokens_in"] += pin
    b["tokens_out"] += pout
    b["calls"] += 1


def snapshot(campaign_id: str | None = None) -> dict[str, float]:
    """Read the current totals without flushing. Used by tests."""
    cid = campaign_id or _current_campaign_id.get()
    if cid is None:
        return {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0}
    return dict(
        _buffer.get(
            cid, {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0}
        )
    )


async def flush_to_db(conn: Any, campaign_id: str) -> None:
    """Write buffered totals to the `campaigns` row and clear the buffer.

    Idempotent: if the buffer is empty (already flushed or never recorded),
    leaves the row untouched. Uses asyncpg's positional `$N` placeholders;
    `campaign_id` is cast to UUID by asyncpg from the string column type.
    """
    import uuid as _uuid

    b = _buffer.pop(campaign_id, None)
    if b is None or b["calls"] == 0:
        return
    await conn.execute(
        """
        UPDATE campaigns
           SET total_cost_usd   = $1,
               total_tokens_in  = $2,
               total_tokens_out = $3
         WHERE id = $4
        """,
        round(b["cost_usd"], 4),
        int(b["tokens_in"]),
        int(b["tokens_out"]),
        _uuid.UUID(campaign_id),
    )


def reset_for_test() -> None:
    """Clear all buffers + context. Tests only — do not call in prod."""
    _buffer.clear()
    _current_campaign_id.set(None)
