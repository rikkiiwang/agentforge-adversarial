"""Unit tests for the Langfuse observability layer.

We don't talk to Langfuse Cloud in CI — the helpers are tested with a
stub client that records the calls instead. The contracts we lock in:

  - No env keys → tracing is a no-op (no SDK init, no client created).
  - With env keys → ``start_campaign_trace`` returns a trace id and
    sets the ContextVar so subsequent ``span()`` / ``record_generation``
    calls nest correctly.
  - SDK call failures degrade silently — a Langfuse outage cannot
    break a campaign.
  - ``flush()`` is safe to call when tracing is disabled.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from agentforge_adversarial import observability


# --- Stub Langfuse client ---

class _StubGeneration:
    def __init__(self, name: str, **kwargs):
        self.name = name
        self.kwargs = kwargs


class _StubSpan:
    def __init__(self, name: str, parent: object | None = None, **kwargs):
        self.name = name
        self.parent = parent
        self.kwargs = kwargs
        self.ended = False
        self.end_kwargs: dict = {}
        self.updates: list[dict] = []
        self.children: list[object] = []

    def span(self, **kwargs):
        name = kwargs.pop("name", "?")
        sp = _StubSpan(name=name, parent=self, **kwargs)
        self.children.append(sp)
        return sp

    def generation(self, **kwargs):
        name = kwargs.pop("name", "?")
        g = _StubGeneration(name=name, **kwargs)
        self.children.append(g)
        return g

    def end(self, **kwargs):
        self.ended = True
        self.end_kwargs = kwargs

    def update(self, **kwargs):
        self.updates.append(kwargs)


class _StubTrace(_StubSpan):
    def __init__(self, name: str, **kwargs):
        super().__init__(name=name, **kwargs)
        self.id = f"trace-{uuid4().hex[:8]}"


class _StubLangfuse:
    def __init__(self):
        self.traces: list[_StubTrace] = []
        self.flushed = 0

    def trace(self, **kwargs):
        name = kwargs.pop("name", "?")
        t = _StubTrace(name=name, **kwargs)
        self.traces.append(t)
        return t

    def flush(self):
        self.flushed += 1


# --- Test fixtures ---

@pytest.fixture(autouse=True)
def _reset_observability_state(monkeypatch):
    """Each test starts with a clean ContextVar + client state."""
    observability.reset_for_test()
    # Default: no Langfuse env keys → tracing disabled. Individual tests
    # opt in by patching the env + injecting a stub client.
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    yield
    observability.reset_for_test()


def _enable_with_stub_client(monkeypatch) -> _StubLangfuse:
    """Set env keys + install a stub client so observability helpers
    have something to talk to without hitting Langfuse Cloud."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    stub = _StubLangfuse()
    # Patch the lazy-init: pretend init succeeded and stash the stub.
    monkeypatch.setattr(observability, "_client", stub)
    monkeypatch.setattr(observability, "_client_init_failed", False)
    return stub


# --- Tests ---

def test_no_keys_means_no_op() -> None:
    """Without LANGFUSE_* env, every helper is a silent no-op."""
    tid = observability.start_campaign_trace(
        campaign_id=uuid4(), target_version="copilot:test"
    )
    assert tid is None
    with observability.span("anything") as sp:
        assert sp is None
        observability.record_generation(model="gpt-4o-mini", tokens_in=10, tokens_out=5)
    observability.flush()  # must not raise


def test_start_campaign_trace_returns_id_and_sets_context(monkeypatch) -> None:
    stub = _enable_with_stub_client(monkeypatch)
    tid = observability.start_campaign_trace(
        campaign_id=uuid4(), target_version="copilot:test", cases_path="evals/cases"
    )
    assert tid is not None
    assert tid.startswith("trace-")
    assert len(stub.traces) == 1
    trace = stub.traces[0]
    assert trace.kwargs["metadata"]["target_version"] == "copilot:test"


def test_span_nests_under_trace(monkeypatch) -> None:
    stub = _enable_with_stub_client(monkeypatch)
    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    with observability.span("red_team_mutator") as sp:
        assert sp is not None
        assert sp.parent is stub.traces[0]
    # Span ended cleanly when the with block exited.
    assert stub.traces[0].children[0].ended is True


def test_nested_spans_inherit_parent(monkeypatch) -> None:
    """A span inside a span nests under the *inner* span, not the trace."""
    stub = _enable_with_stub_client(monkeypatch)
    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    with observability.span("outer"):
        with observability.span("inner") as inner:
            assert inner is not None
            assert inner.parent.name == "outer"


def test_record_generation_attaches_to_current_span(monkeypatch) -> None:
    stub = _enable_with_stub_client(monkeypatch)
    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    with observability.span("red_team_mutator"):
        observability.record_generation(
            model="claude-haiku-4-5-20251001",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.0003,
        )
    mutator_span = stub.traces[0].children[0]
    gens = [c for c in mutator_span.children if isinstance(c, _StubGeneration)]
    assert len(gens) == 1
    assert gens[0].kwargs["model"] == "claude-haiku-4-5-20251001"
    assert gens[0].kwargs["usage"]["input"] == 100
    assert gens[0].kwargs["usage"]["output"] == 50


def test_record_generation_attaches_to_trace_when_no_span(monkeypatch) -> None:
    """An LLM call outside any explicit span attaches at trace level."""
    stub = _enable_with_stub_client(monkeypatch)
    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    observability.record_generation(model="gpt-4o-mini", tokens_in=10, tokens_out=5)
    gens = [c for c in stub.traces[0].children if isinstance(c, _StubGeneration)]
    assert len(gens) == 1


def test_span_exception_marks_error_and_reraises(monkeypatch) -> None:
    """A node that raises inside the with block must propagate the
    exception (caller's error path stays intact) but the span gets
    marked as ERROR so reviewers see it in Langfuse."""
    stub = _enable_with_stub_client(monkeypatch)
    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    with pytest.raises(RuntimeError, match="boom"):
        with observability.span("dispatch"):
            raise RuntimeError("boom")
    dispatch_span = stub.traces[0].children[0]
    assert dispatch_span.ended is True
    assert dispatch_span.end_kwargs.get("level") == "ERROR"


def test_annotate_current_span_records_output(monkeypatch) -> None:
    stub = _enable_with_stub_client(monkeypatch)
    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    with observability.span("synthesize"):
        observability.annotate_current_span(
            output="96 → 10", metadata={"k": 10}
        )
    span = stub.traces[0].children[0]
    assert len(span.updates) == 1
    assert span.updates[0]["output"] == "96 → 10"


def test_sdk_failure_does_not_propagate(monkeypatch) -> None:
    """If the Langfuse SDK raises while creating a generation, the
    caller doesn't see it — observability is best-effort, never
    load-bearing."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    class _ExplodingTrace(_StubTrace):
        def generation(self, **kwargs):
            raise RuntimeError("langfuse cloud is down")

    class _ExplodingClient(_StubLangfuse):
        def trace(self, **kwargs):
            name = kwargs.pop("name", "?")
            t = _ExplodingTrace(name=name, **kwargs)
            self.traces.append(t)
            return t

    monkeypatch.setattr(observability, "_client", _ExplodingClient())
    monkeypatch.setattr(observability, "_client_init_failed", False)

    observability.start_campaign_trace(campaign_id=uuid4(), target_version="t")
    # Must not raise.
    observability.record_generation(model="gpt-4o-mini", tokens_in=1, tokens_out=1)


def test_flush_is_noop_when_disabled() -> None:
    observability.flush()  # no env keys, no crash
