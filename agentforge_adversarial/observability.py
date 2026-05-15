"""Langfuse tracing — per-campaign trace, per-node spans, per-LLM generations.

ARCHITECTURE.md §5 (Observability layer): "Per-agent traces, token
spans, cost spans, inter-agent message log." The platform produces one
``trace`` per campaign and nests per-agent ``spans`` underneath
(orchestrator, mutator, synthesize, dispatch, judge, class-probe,
documentation-agent). Each LLM call inside a span emits a Langfuse
``generation`` carrying model + token counts + cost — same data the
in-memory cost rollup tracks, but with per-call resolution.

Degradation discipline:
  - No env keys → no-op tracing. The harness still runs; observability
    is best-effort, never load-bearing.
  - Langfuse SDK call raises → log + continue. A trace export failure
    cannot break a campaign.
  - Process exit → ``flush()`` drains the Langfuse SDK's internal
    buffer before the asyncpg pool drains, so events ship before exit.

Threading: spans / generations are scoped via ContextVar (same pattern
``cost.set_campaign`` uses) so an asyncio.gather of mutator calls all
attach generations to the right parent span without parameter-threading
through every helper signature.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from typing import Any
from uuid import UUID

logger = logging.getLogger("agentforge.observability")


# ContextVars: the active Langfuse trace + span for the current asyncio
# task. Defaults to None so any code that runs outside a campaign trace
# (e.g. tests) silently no-ops on every helper call.
_current_trace: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "agentforge_lf_trace", default=None
)
_current_span: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "agentforge_lf_span", default=None
)

# Process-wide Langfuse client; lazy-initialized on first use so importing
# this module doesn't touch the network or require env vars.
_client: Any | None = None
_client_init_failed = False


def _get_client() -> Any | None:
    """Lazy singleton. Returns None when tracing is disabled (no keys,
    SDK import fails, or a prior init crashed). Idempotent."""
    global _client, _client_init_failed
    if _client is not None or _client_init_failed:
        return _client
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not pk or not sk:
        # No keys configured → tracing intentionally disabled. Cache the
        # decision so we don't re-check on every helper call.
        _client_init_failed = True
        return None
    try:
        from langfuse import Langfuse  # type: ignore
    except ImportError:
        logger.warning("langfuse package missing; tracing disabled")
        _client_init_failed = True
        return None
    try:
        _client = Langfuse(
            public_key=pk,
            secret_key=sk,
            host=os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
        )
        return _client
    except Exception as e:
        logger.warning("Langfuse init failed; tracing disabled: %r", e)
        _client_init_failed = True
        return None


def start_campaign_trace(
    *,
    campaign_id: UUID,
    target_version: str,
    cases_path: str | None = None,
) -> str | None:
    """Create a trace for one campaign run. Returns the trace id, or
    None if tracing is disabled. The id is stored on the ``campaigns``
    row by the runner so the dashboard can deep-link to Langfuse Cloud.

    Sets the trace into the current ContextVar so every subsequent
    ``span()`` and ``record_generation()`` attaches under it without
    threading the trace object through every signature.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        trace = client.trace(
            name=f"campaign-{str(campaign_id)[:8]}",
            user_id="agentforge-harness",
            metadata={
                "campaign_id": str(campaign_id),
                "target_version": target_version,
                "cases_path": cases_path or "",
            },
            tags=["adversarial-campaign"],
        )
        _current_trace.set(trace)
        return trace.id
    except Exception as e:
        logger.warning("[lf] start_campaign_trace failed: %r", e)
        return None


@contextlib.contextmanager
def span(name: str, *, input_summary: str | None = None) -> Any:
    """Context manager for a per-node span. Nests under the active
    trace; the span itself becomes the active parent for any
    ``record_generation`` calls inside the ``with`` block.

    Yields the span (or None when tracing is disabled) so callers can
    set ``output`` / metadata as the node completes.
    """
    trace = _current_trace.get()
    if trace is None:
        # No active trace → yield None; helpers downstream become no-ops.
        yield None
        return
    sp: Any | None = None
    parent_span = _current_span.get()
    token = None
    try:
        # Span lives on the parent (span if nested, otherwise trace).
        parent = parent_span if parent_span is not None else trace
        sp = parent.span(name=name, input=input_summary)
        token = _current_span.set(sp)
        yield sp
    except Exception as e:
        # Re-raise so the caller's exception path stays intact, but
        # mark the span as errored so it's visible in Langfuse.
        if sp is not None:
            try:
                sp.end(level="ERROR", status_message=repr(e))
            except Exception:
                pass
        raise
    else:
        if sp is not None:
            try:
                sp.end()
            except Exception as e:
                logger.debug("[lf] span.end failed: %r", e)
    finally:
        if token is not None:
            _current_span.reset(token)


def record_generation(
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float = 0.0,
    input_summary: str | None = None,
    output_summary: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Emit a Langfuse generation under the current span (or trace).

    Called alongside ``cost.record_usage`` from each LLM call site. No-op
    when no trace is active. ``input_summary`` / ``output_summary`` may
    be truncated payloads — Langfuse stores them verbatim, so callers
    should pre-trim to avoid shipping multi-KB prompts on every span.
    """
    trace = _current_trace.get()
    if trace is None:
        return
    parent = _current_span.get() or trace
    try:
        parent.generation(
            name=f"llm:{model}",
            model=model,
            input=input_summary,
            output=output_summary,
            usage={
                "input": tokens_in,
                "output": tokens_out,
                "total": tokens_in + tokens_out,
                "unit": "TOKENS",
            },
            metadata={**(metadata or {}), "cost_usd": round(cost_usd, 6)},
        )
    except Exception as e:
        logger.debug("[lf] record_generation failed: %r", e)


def annotate_current_span(
    *,
    output: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Attach output / metadata to the active span after it's done work.

    Used by graph nodes to record their per-node outcome (e.g. mutate
    node emits ``output="42 candidates synthesized to 10"``).
    """
    sp = _current_span.get()
    if sp is None:
        return
    try:
        sp.update(output=output, metadata=metadata or {})
    except Exception as e:
        logger.debug("[lf] annotate_current_span failed: %r", e)


def flush() -> None:
    """Drain the Langfuse SDK's internal buffer.

    Called by the runner in its ``finally`` block, before the asyncpg
    pool is closed, so events ship before the process exits. Safe to
    call when tracing is disabled.
    """
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as e:
        logger.debug("[lf] flush failed: %r", e)


def reset_for_test() -> None:
    """Clear the process-wide client + ContextVars. Tests only — do not
    call in prod."""
    global _client, _client_init_failed
    _client = None
    _client_init_failed = False
    _current_trace.set(None)
    _current_span.set(None)
