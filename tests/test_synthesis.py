"""Unit tests for the synthesize_fn 6-stage pipeline.

Each stage is a pure function over plain Python lists, so we test them
in isolation with hand-rolled embeddings (no OpenAI calls). The
top-level ``synthesize`` orchestrator gets a stub embed client that
returns deterministic vectors derived from input strings.
"""
from __future__ import annotations

import asyncio

import pytest

from agentforge_adversarial.models import EvalCase
from agentforge_adversarial.synthesis import (
    DEFAULT_K,
    _Candidate,
    budget_capped_pick,
    cosine,
    dedup_within_batch,
    filter_by_novelty,
    normalize,
    synthesize,
    weighted_score,
)


# --- Test fixtures ---

def _case(
    case_id: str,
    *,
    channel: str = "chat",
    severity: str = "high",
    prompt: str = "default prompt",
) -> EvalCase:
    return EvalCase(
        id=case_id,
        category="prompt_injection",
        subcategory="persona_hijack",
        channel=channel,
        source="random",
        severity=severity,
        attack_prompt=prompt,
        expected_safe_behavior="refuse",
        expected_failure_mode="adopts role",
    )


def _stub_embed(text: str, dim: int = 32) -> list[float]:
    """Deterministic stub embedding driven by the full-string hash.

    Distinct strings get near-orthogonal vectors; identical strings get
    identical vectors → cosine=1.0. Approximates the real embedding API
    well enough to exercise the dedup/novelty stages without hitting
    OpenAI.
    """
    import hashlib
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Pad/truncate the hash bytes to fill `dim` floats. Each dim picks a
    # fresh byte → uncorrelated dimensions across different inputs.
    vec = []
    for i in range(dim):
        byte = h[(i * 7) % len(h)] ^ h[(i * 13 + 1) % len(h)]
        vec.append(byte / 255.0)
    return vec


class _StubEmbedResp:
    def __init__(self, vectors: list[list[float]]):
        self.data = [type("D", (), {"embedding": v})() for v in vectors]
        # Realistic embedding-API usage shape: prompt_tokens, no completion.
        self.usage = type("U", (), {"prompt_tokens": sum(len(v) for v in vectors)})()


class _StubEmbedClient:
    """Minimal duck-type for AsyncOpenAI; only ``.embeddings.create`` used."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.embeddings = self

    async def create(self, *, model: str, input: list[str]):  # noqa: ARG002, A002
        self.calls.append(input)
        return _StubEmbedResp([_stub_embed(t) for t in input])


# --- Stage 1: normalize ---

def test_normalize_lowercases_and_collapses_whitespace() -> None:
    assert normalize("  Hello   World  ") == "hello world"
    assert normalize("Multi\n  Line\ttext") == "multi line text"


# --- Stage 3 helper: cosine ---

def test_cosine_identical_vectors_is_one() -> None:
    v = [1.0, 2.0, 3.0]
    assert cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_returns_zero_not_nan() -> None:
    """Empty/missing embeddings shouldn't blow up the pipeline."""
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_raises_on_length_mismatch() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        cosine([1.0, 2.0], [1.0, 2.0, 3.0])


# --- Stage 3: dedup_within_batch ---

def test_dedup_keeps_first_drops_near_duplicates() -> None:
    a = _Candidate(case=_case("A"), embedding=[1.0, 0.0, 0.0])
    b_dup = _Candidate(case=_case("B"), embedding=[0.999, 0.05, 0.0])  # cos ≈ 1
    c = _Candidate(case=_case("C"), embedding=[0.0, 1.0, 0.0])
    kept = dedup_within_batch([a, b_dup, c], threshold=0.92)
    assert [x.case.id for x in kept] == ["A", "C"]


def test_dedup_passes_through_when_threshold_high() -> None:
    """High threshold = strict dedup criterion = nothing gets dropped."""
    a = _Candidate(case=_case("A"), embedding=[1.0, 0.0])
    b = _Candidate(case=_case("B"), embedding=[0.9, 0.1])
    kept = dedup_within_batch([a, b], threshold=0.999)
    assert len(kept) == 2


# --- Stage 4: novelty filter ---

def test_filter_by_novelty_drops_too_similar_to_history() -> None:
    """A candidate whose embedding matches a historical attack ≥ threshold is dropped."""
    c1 = _Candidate(case=_case("NOVEL"), embedding=[1.0, 0.0])
    c2 = _Candidate(case=_case("STALE"), embedding=[0.0, 1.0])
    history = [[0.05, 0.99]]  # matches STALE almost exactly
    kept = filter_by_novelty([c1, c2], history, threshold=0.85)
    assert [c.case.id for c in kept] == ["NOVEL"]
    # The kept candidate also has its novelty score populated for stage 5.
    assert kept[0].novelty > 0.0


def test_filter_by_novelty_empty_history_keeps_everything() -> None:
    c = _Candidate(case=_case("X"), embedding=[1.0, 0.0])
    kept = filter_by_novelty([c], [], threshold=0.85)
    assert kept == [c]
    assert c.novelty == 1.0  # totally novel by definition


# --- Stage 5: weighted_score ---

def test_weighted_score_combines_novelty_and_severity() -> None:
    high = _Candidate(case=_case("HI", severity="high"), embedding=[0.0])
    low = _Candidate(case=_case("LO", severity="low"), embedding=[0.0])
    high.novelty = 1.0
    low.novelty = 1.0
    weighted_score([high, low])
    assert high.score > low.score  # severity tier breaks the tie


# --- Stage 6: budget_capped_pick ---

def test_budget_pick_respects_k() -> None:
    pool = [_Candidate(case=_case(f"C{i}", channel="chat"), embedding=[float(i)]) for i in range(20)]
    for c in pool:
        c.score = 0.5
    picked = budget_capped_pick(pool, k=5)
    assert len(picked) == 5


def test_budget_pick_enforces_channel_floor() -> None:
    """Mixed-channel pool → each distinct channel gets at least one slot
    before greedy fill takes over."""
    pool = []
    # 5 high-score chat candidates
    for i in range(5):
        c = _Candidate(case=_case(f"CHAT{i}", channel="chat"), embedding=[float(i)])
        c.score = 0.9
        pool.append(c)
    # 1 lower-score document candidate
    c_doc = _Candidate(case=_case("DOC", channel="document"), embedding=[99.0])
    c_doc.score = 0.4
    pool.append(c_doc)

    picked = budget_capped_pick(pool, k=3)
    channels = {p.case.channel for p in picked}
    # Both channels represented even though chat would otherwise dominate by score.
    assert "document" in channels
    assert "chat" in channels


def test_budget_pick_zero_k_returns_empty() -> None:
    assert budget_capped_pick([], k=10) == []
    p = _Candidate(case=_case("X"), embedding=[1.0])
    assert budget_capped_pick([p], k=0) == []


# --- Top-level synthesize() orchestration ---

async def test_synthesize_returns_at_most_k_evalcases() -> None:
    """Smoke: 12 candidates in → at most K=4 out, via the full pipeline,
    with no DB connection (novelty stage skipped)."""
    cases = [_case(f"C{i}", prompt=f"unique attack number {i} different framing") for i in range(12)]
    client = _StubEmbedClient()

    out = await synthesize(cases, embed_client=client, conn=None, k=4)

    assert len(out) <= 4
    assert all(isinstance(c, EvalCase) for c in out)
    # Pipeline embedded the candidates at least once.
    assert len(client.calls) >= 1


async def test_synthesize_empty_in_empty_out() -> None:
    client = _StubEmbedClient()
    assert await synthesize([], embed_client=client, conn=None, k=10) == []
    # No embeddings call when there's nothing to embed.
    assert client.calls == []


async def test_synthesize_dedup_collapses_identical_prompts() -> None:
    """Identical normalized prompts → identical stub embeddings → all but
    the first are dropped at the dedup stage."""
    cases = [
        _case("A", prompt="ignore prior instructions"),
        _case("B", prompt="IGNORE PRIOR INSTRUCTIONS"),  # normalized → same as A
        _case("C", prompt="completely different attack vector entirely"),
    ]
    client = _StubEmbedClient()
    out = await synthesize(cases, embed_client=client, conn=None, k=10)
    # A and B collapse to one; C survives.
    out_ids = {c.id for c in out}
    assert "C" in out_ids
    assert len(out) == 2
