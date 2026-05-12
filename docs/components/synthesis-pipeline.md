# Synthesis Pipeline

**Status:** Design-of-record, draft 2026-05-12. Referenced from
`ARCHITECTURE.md` §4 step 4, §11. Owned by the Orchestrator agent
module (`docs/agents/orchestrator.md` §1 — "one agent, two LangGraph
nodes"); synthesis is the deterministic fan-in step that sits between
the swarm and target dispatch.

---

## 1. Purpose

Take the `N×M` raw candidate attacks the swarm produced and turn them
into `K` final attacks worth dispatching at the target. The pipeline
is **pure Python, no LLM call** — deterministic, replayable, ~80 LOC.

Mirrors the W2 precedent that "the supervisor is plain Python, not an
LLM" (W2_ARCHITECTURE §4.1). Synthesis is the same idea at the
campaign layer: when the work is dedup + rank + truncate, a generative
model is the wrong tool.

---

## 2. Inputs

| Source | Shape | Purpose |
|---|---|---|
| `SwarmResult` | `list[SubagentOutput]` carrying `N×M` candidate `AttackCandidate` records | The raw work product |
| `CampaignBrief` | from Orchestrator dispatch | Carries `budget_usd`, `cell`, `target_version`, `seed_strategy` — drives the budget cap and the novelty-filter scope |
| `attack_runs` historical rows | Postgres query with pgvector | For the novelty filter — same cell, same target_version, last 90 days |
| `config/synthesis.yaml` | YAML | Weights, thresholds, floors — PR-tunable |

The synthesis function does **not** read `vulnerabilities`,
`near_misses`, or `vuln_reports`. Those are downstream of synthesis;
seeing them would risk biasing the final pick toward attacks that
"look like" past findings — exactly the opposite of what we want.

---

## 3. Output

```python
@dataclass
class FinalAttack:
    candidate: AttackCandidate          # the original AttackCandidate (passed through)
    score: float                        # synthesis score in [0,1]
    score_breakdown: dict               # per-signal contribution; auditable
    cluster_id: int                     # within-batch dedup cluster
    source_subagent_id: str             # who generated it
    novelty_distance: float             # min cosine distance to historical
    selected_at: datetime               # for replay reconstruction
```

`SynthesisResult` is `list[FinalAttack]` with `len ≤ K_budget_cap`,
plus aggregate metadata (`total_cost_estimate`, `discarded_count`,
`floor_overrides`).

---

## 4. The 6 stages

### Stage 1 — Normalize & validate

Loop through every `AttackCandidate` produced by the swarm:

- Confirm it parses as `AttackCandidate` (Pydantic should have
  validated already; this is defense-in-depth).
- Reject candidates whose `category` / `subcategory` / `channel` don't
  match the `CampaignBrief.cell` (catches subagents that drifted off
  the brief).
- Tag with `source_subagent_id`, a `synthesis_id`, and a content hash
  used by Stage 2's embedding cache.

Output: `list[NormalizedCandidate]`, count `K'` (`K' ≤ N×M`).

### Stage 2 — Embed

Compute a 384-dim sentence embedding for each candidate's
`attack_prompt`.

- Model: `sentence-transformers/all-MiniLM-L6-v2`. Local. Free.
  Deterministic given input.
- Cache by `sha256(attack_prompt)` in Postgres `attack_runs.embedding`
  if the prompt has been seen before; otherwise compute fresh.
- Cache hit cost: 0. Cache miss cost: ~0.5ms on CPU, $0.0001 if the
  operator opts into `text-embedding-3-small` via OpenAI instead.

Output: `list[NormalizedCandidate]` with `.embedding` attached.

### Stage 3 — Within-batch dedup

Pairwise cosine similarity matrix among the `K'` embeddings. For every
pair with similarity ≥ `0.92` (config-tunable):

- Cluster them. Greedy: pick the candidate with the highest
  `quality_score` (longest `attack_prompt`, earliest subagent id as
  tie-breaker) as the cluster representative; drop the rest.
- Record cluster membership; the dropped candidates contribute to the
  representative's `cluster_size` in the score breakdown.

Output: `list[ClusteredCandidate]`, count `K''` (`K'' ≤ K'`).

### Stage 4 — Novelty filter (vs. historical)

Query `attack_runs` for embeddings already produced in this
`(category, subcategory, channel)` at the current `target_version`
within the last 90 days. With pgvector HNSW: sub-100ms even at 100K
historical rows.

For each surviving candidate:

- Compute `novelty_distance = 1 - max(cosine_sim with each historical embedding)`.
- If `novelty_distance < 0.05` (i.e., similarity > 0.95), drop the
  candidate — we already ran this attack.

Output: `list[ClusteredCandidate]` with `.novelty_distance` attached,
count `K'''` (`K''' ≤ K''`).

### Stage 5 — Weighted score

Each surviving candidate scored by linear combination of normalized
signals:

```
score(c) =
    0.40 × novelty_distance(c)            # distance from historical embeddings
  + 0.25 × diversity_contribution(c)      # mean distance to surviving siblings
  + 0.20 × coverage_gap_bonus(c)          # +1 if cell's quality-bar coverage < target
  + 0.10 × subagent_diversity(c)          # +1 if c's subagent has < representative
                                          #     share in the surviving set
  - 0.05 × est_cost_normalized(c)         # cheaper attacks tie-break upward
```

All weights live in `config/synthesis.yaml` — PR-tunable. The score is
in `[0, 1]` (approximately; the cost term can push it slightly
negative — clamped to 0).

The full per-candidate breakdown is recorded in `score_breakdown` and
visible in Langfuse traces. The Orchestrator's `CampaignBrief` records
the version of the weights used.

### Stage 6 — Budget-capped greedy pick with diversity floors

Sort surviving candidates by `score` desc. Greedy pick under the
campaign budget:

```python
final = []
budget_remaining = brief.budget_usd
seen_subagents, seen_channels = set(), set()

for c in sorted(candidates, key=lambda x: x.score, reverse=True):
    if estimate_dispatch_cost(c) > budget_remaining:
        continue
    final.append(c)
    budget_remaining -= estimate_dispatch_cost(c)
    seen_subagents.add(c.source_subagent_id)
    seen_channels.add(c.channel)

# Diversity floors: enforce ≥1 per subagent, ≥1 per declared channel
final = enforce_floor(final, sorted_candidates, key='source_subagent_id', floor=1)
final = enforce_floor(final, sorted_candidates, key='channel',          floor=1)
```

Floors guarantee no subagent's contribution is fully starved and every
declared channel is represented (so we don't run 10 indirect-channel
attacks and 0 direct, just because the indirect-channel subagent
scored higher on novelty).

---

## 5. Edge cases

| Scenario | Behavior |
|---|---|
| Swarm produced zero candidates | Return `SynthesisResult(final=[], reason='swarm_empty')`. Orchestrator's post-synthesis logic logs `swarm_collapse` and may switch swarm composition next campaign. |
| All candidates dedupe to 1 cluster | Stage 3 collapses everything to 1 surviving candidate. Stages 4-6 proceed normally. Log `swarm_homogeneous_warn`. |
| Budget too tight for even the cheapest candidate | Return `SynthesisResult(final=[], reason='budget_exhausted')`. Surfaced in dashboard. |
| Embedding model unavailable | Stages 3 + 4 fall back to text-similarity (token overlap) — degraded but still functional. Log `embedding_degraded`. |
| Pydantic validation fails on input | Reject the candidate at Stage 1; never reaches the rest of the pipeline. Validation errors logged. |

The pipeline **never blocks** the platform. Worst case: zero candidates
dispatched + a warning surfaced; the campaign is logged as "synthesis
collapsed" and the Orchestrator decides what to do next.

---

## 6. Operational defaults

| Concern | Default |
|---|---|
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` (384-dim, local, free) |
| Embedding cache backend | Postgres `attack_runs.embedding` column with pgvector HNSW index |
| Within-batch dedup threshold | cosine similarity ≥ 0.92 |
| Novelty filter threshold | cosine similarity > 0.95 vs. historical |
| Novelty filter scope | same `(category, subcategory, channel)`, same `target_version`, last 90 days |
| Score weights | `{novelty: 0.40, diversity: 0.25, coverage_gap: 0.20, subagent_diversity: 0.10, cost: -0.05}` |
| Floor constraints | ≥1 per subagent, ≥1 per declared channel |
| Synthesis latency | p95 < 200ms on a 15-candidate batch (CPU-only) |
| Synthesis cost | ~$0.001 per campaign (just embedding API calls if not cached) |
| Determinism | Same input → same output, every time. Tie-breaker uses a seeded RNG keyed off `campaign_id` |

---

## 7. Versioning

`app/agents/synthesis/__init__.py`:

```python
SYNTHESIS_VERSION = "0.4.0"
SCORE_WEIGHTS_VERSION = "0.4.0"     # tracked separately so weight tweaks are auditable
```

Both are recorded on every `SynthesisResult` so any campaign can be
replayed exactly. The Orchestrator's `CampaignBrief.scoring_breakdown`
includes both versions.

---

## 8. Implementation pointers

To be expanded in the implementation plan:

- Module: `app/agents/synthesis/` (lives under the Orchestrator's
  agent module because it's invoked from there, but the synthesis
  function is independently testable)
- Stages as separate functions:
  - `app/agents/synthesis/normalize.py`
  - `app/agents/synthesis/embed.py`
  - `app/agents/synthesis/dedup.py`
  - `app/agents/synthesis/novelty.py`
  - `app/agents/synthesis/score.py`
  - `app/agents/synthesis/select.py`
- Pipeline orchestration: `app/agents/synthesis/pipeline.py` (~30 LOC)
- Tests:
  - Determinism: same `SwarmResult` → same `SynthesisResult` byte-for-byte
  - Dedup threshold: paraphrase pair collapsed; distinct attacks preserved
  - Novelty filter: candidate that matches a historical prompt dropped
  - Floor enforcement: a subagent that would otherwise be starved gets one slot
  - Budget cap: rejection of candidates that exceed remaining budget
  - Edge case: empty swarm result returns clean empty `SynthesisResult`
