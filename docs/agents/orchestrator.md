# Orchestrator Agent

**Status:** Design-of-record, draft 2026-05-12. Referenced from
`ARCHITECTURE.md` §2, §4 steps 2 and 7, §13.

---

## 1. Purpose

Decide what attack to run next and route post-verdict signals. The
Orchestrator is the **strategic brain** of the platform — it reads the
full state (heat-map, near-misses, vulns, regression schedule, cost
ledger) and picks the highest-leverage campaign cell to probe. After
the Judge writes a verdict, the Orchestrator is re-invoked to route
the signal (FAIL → 3-way fan-out, PARTIAL → mutator loop, PASS → bump
coverage and move on).

Two non-negotiables from the design history:

- **Pure-Python decision logic** (no LLM call). Matches the
  `synthesize_fn` precedent. PRD pitfall #3 mitigation: "supervisor is
  plain Python." Replayable, auditable, deterministic.
- **One agent, two LangGraph nodes** — `orchestrator_dispatch` (pre-
  swarm) and `orchestrator_postverdict` (post-Judge). Same module,
  different state inputs.

---

## 2. Inputs

| Source | When read | Purpose |
|---|---|---|
| `threat_model_cells` | Every dispatch | The denominator. Defines all `(category, subcategory, channel)` cells eligible for probing. |
| Coverage rollup query over `attack_runs` | Every dispatch | Per-cell verdict counts (pass / partial / fail) at the current `target_version` |
| `near_misses` table | Every dispatch | Active partials (state=`exploring`); informs `partial_rate` signal |
| `vulnerabilities` table | Every dispatch | Open vulns per category; influences `severity_baseline` weighting |
| `regression_schedule` table | Every dispatch | Hard constraint: any overdue replay forces an immediate campaign |
| Target `/healthz` endpoint | Once per dispatch | Detects target_version (git SHA) change → triggers full sweep |
| `config/orchestrator.yaml` | Every dispatch | Scoring weights, per-category budget pools, cooldowns |
| `config/swarm.yaml` | Every dispatch | Operator-owned swarm defaults (read-only by Orchestrator) |
| Cost ledger (synthesized from `attack_runs.cost_usd`) | Every dispatch | Per-category daily/weekly burn vs. budget pool |
| Judge verdict (post-verdict invocation only) | Post-Judge | Routes signal to the appropriate downstream branch |

---

## 3. Outputs

The Orchestrator's outputs are entirely structured data, no prose
generation. Two output shapes by invocation type:

**`orchestrator_dispatch` emits a `CampaignBrief`:**

```python
@dataclass
class CampaignBrief:
    campaign_id: UUID
    cell: tuple[str, str, str]                  # (category, subcategory, channel)
    target_version: str                         # git SHA from /healthz
    budget_usd: float                           # campaign budget cap
    swarm_recommendation: SwarmRecommendation   # see §7
    seed_strategy: Literal['random', 'mutator', 'direct', 'regression']
    seed_attack_ids: list[UUID]                 # for mutator/regression sources
    scoring_breakdown: dict                     # transparent: why this cell won
    trigger: Literal['scoring', 'regression_due', 'new_target_version', 'manual']
```

The brief is consumed by the Red Team Swarm dispatcher node downstream.
It's also written verbatim to a `campaigns` table for auditability (so
"why did the platform run this on 2026-05-15?" is always answerable).

**`orchestrator_postverdict` writes downstream-routing signals:**

| Judge verdict | Action |
|---|---|
| `pass` | INSERT no new row (verdict already in `attack_runs`); update per-cell coverage counters; trigger next campaign dispatch if budget remains |
| `partial` | INSERT `near_misses` row (state=`exploring`); enqueue mutator round for the next dispatch |
| `fail` | Three parallel branches: dispatch Documentation Agent node · INSERT `regression_schedule` row · dispatch Class-probe mutator. (See `ARCHITECTURE.md` §4 step 7c.) |

The Orchestrator does **not** write to `vulnerabilities`,
`vuln_reports`, or `regression_schedule.last_verdict` directly. Those
writes belong to the Documentation Agent and Regression Harness.

---

## 4. The two LangGraph invocations

```
                       ┌────────────────────────────────┐
   target_version  ───▶│   orchestrator_dispatch        │
   regression_cron ───▶│   (pre-swarm, picks the cell)  │
   manual button  ───▶ │                                │
                       └───────────────┬────────────────┘
                                       │ CampaignBrief
                                       ▼
                       ┌────────────────────────────────┐
                       │   Red Team swarm + synthesis   │
                       └───────────────┬────────────────┘
                                       │ K final attacks
                                       ▼
                       ┌────────────────────────────────┐
                       │   target → Judge               │
                       └───────────────┬────────────────┘
                                       │ verdict
                                       ▼
                       ┌────────────────────────────────┐
                       │   orchestrator_postverdict     │
                       │   (routes signal downstream)   │
                       └───────────────┬────────────────┘
                                       │
                                       ▼ (loops to dispatch
                                          when budget remains)
```

Same Python module, two different entry points. State carries the
`CampaignBrief` between invocations so the post-verdict node knows
which cell to update.

---

## 5. Scoring scheme — 5 weighted signals + 2 hard constraints

### Hard constraints (override score; processed first)

The Orchestrator checks these before any scoring. If any hard
constraint fires, scoring is bypassed and the constraint dictates the
campaign:

| Constraint | Trigger | Resulting campaign |
|---|---|---|
| `regression_overdue` | `regression_schedule.last_run_at + rerun_interval_days < now()` for any enabled row | Force-pick that row's `(category, subcategory, channel)`; `trigger='regression_due'`; `seed_strategy='regression'` |
| `new_target_version` | Last campaign's `target_version` ≠ current `/healthz` SHA | Force a full sweep — iterate every previously-tested cell and queue regression-style campaigns until all are re-validated against the new version. The Orchestrator emits a `version_sweep_<sha>` plan; campaigns dispatch in priority order from there |

These two constraints encode promises the platform makes to its users:
"every fix gets validated weekly" and "every deploy gets re-tested."

### Soft signals (weighted sum when no hard constraint fires)

Per-cell score = weighted linear combination of normalized signals. All
weights live in `config/orchestrator.yaml` — PR-tunable.

| Signal | Weight (default) | What it captures | Higher score when… |
|---|---|---|---|
| `coverage_gap` | 0.40 | How under-tested the cell is | Cell has 0 or few attempts at current target_version |
| `partial_rate` | 0.25 | Wavering signal | Cell's recent PARTIAL rate is high — system is cracking here |
| `severity_baseline` | 0.15 | From `threat_model_cells.severity_baseline` | CRITICAL / HIGH cells prioritized over MED / LOW |
| `staleness` | 0.10 | Time since last campaign on this cell | Encourages cycling; prevents over-focus |
| `diversity_score` | 0.10 | Embedding spread of attacks already attempted | Low diversity → cell deserves more variety |

Computed signal values are normalized to `[0, 1]` before weighting.
The total score is a scalar in `[0, 1]`. The cell with the highest
score is picked; ties broken by `staleness` desc then random seed.

The full scoring breakdown is written verbatim into
`CampaignBrief.scoring_breakdown` so every campaign's reasoning is
inspectable in Langfuse and the dashboard.

### Example `config/orchestrator.yaml`

```yaml
scoring_weights:
  coverage_gap: 0.40
  partial_rate: 0.25
  severity_baseline: 0.15
  staleness: 0.10
  diversity_score: 0.10

hard_constraints:
  regression_overdue: true
  new_target_version: true

cooldowns:
  per_cell_min_seconds: 600       # 10 min between campaigns on the same cell
  category_cycle_target: 6        # try to touch all categories within 6 campaigns

tie_breaking:
  primary: staleness_desc
  fallback: random_seed
```

---

## 6. Budget allocation — per-category caps + score-weighted within cap

Three layers, computed top-down:

**Layer 1 — Per-category pool** (operator-configured)

```yaml
# in config/orchestrator.yaml
category_budgets:
  default_daily_usd: 5.00                  # MVP default
  per_category_overrides:
    verification_bypass: 10.00             # ⭐ category — higher pool
    multimodal_poisoning: 8.00
    observability_leak: 6.00
    dos_cost: 2.00                         # cheap to test
```

**Layer 2 — Per-campaign budget within the pool**

When a cell is picked, the campaign budget is:

```
budget_usd = max(MIN_CAMPAIGN, normalized_score × remaining_category_pool)
```

`MIN_CAMPAIGN` defaults to `$0.50` so even low-score campaigns get
enough budget to be meaningful. `remaining_category_pool` decrements
as the day progresses; when it hits zero, that category deprioritizes
for the rest of the day.

**Layer 3 — Per-attack run cap** (already designed)

The Judge enforces `$0.02` single-judge / `$0.05` ensemble per attack
(documented in `docs/agents/judge.md` §10). Class-probe and mutator
spawn additional `attack_runs` rows that consume the campaign budget
until exhausted.

A category that produces zero findings for two consecutive days has
its daily pool halved (configurable). A category that produces a
CRITICAL finding gets its pool doubled for the next 7 days. Both
adjustments are computed by the Orchestrator and persisted in
`config/orchestrator.yaml` via a controlled update — operator can
override at any time.

---

## 7. Swarm configuration — operator-owned, agent-recommended

**Source of truth: `config/swarm.yaml`** is operator-authored. The
Orchestrator never auto-rewrites it. Example:

```yaml
defaults:
  subagent_count: 3
  composition:
    - id: subagent-llama
      model: llama-3-70b-uncensored
      provider: ollama
      temperature: 0.8
      prompt_template: prompts/red_team/llama_v1.txt
    - id: subagent-deepseek
      model: deepseek-r1-7b
      provider: ollama
      temperature: 0.7
      prompt_template: prompts/red_team/deepseek_v1.txt
    - id: subagent-mistral
      model: mistral-7b-instruct
      provider: ollama
      temperature: 0.9
      prompt_template: prompts/red_team/mistral_v1.txt
  per_subagent_attacks: 5            # M attacks each → N×M = 15 candidates

per_category_overrides:
  multimodal_poisoning:               # needs vision capability
    composition:
      - id: subagent-claude-vision
        model: claude-sonnet-4-6
        provider: anthropic
        temperature: 0.7
        prompt_template: prompts/red_team/vision_v1.txt
      # ... 2 more
```

**The Orchestrator's role: recommend, not decide.** On every dispatch
the Orchestrator computes a `SwarmRecommendation`:

```python
@dataclass
class SwarmRecommendation:
    proposed_composition: list[SubagentSpec]   # what Orchestrator thinks would work best
    rationale: list[str]                       # why each subagent was suggested
    base_config_source: str                    # which YAML section was the starting point
```

How the recommendation is computed (pure-Python, no LLM):

- **Start from `config/swarm.yaml`** — pick `per_category_overrides[category]` if it exists, otherwise `defaults`
- **Apply historical performance bias** (post-MVP): if model M has a >2× higher FAIL rate in this category than the average, the recommendation upweights M
- **Apply diversity constraint**: at least 2 distinct model families in the recommendation
- **Apply cost constraint**: estimated swarm cost ≤ campaign budget × 0.6 (leaves headroom for target dispatch + Judge)

The recommendation lands in the **dashboard** alongside the campaign
brief. Operator can:

- **Accept** (default if no action within `auto_dispatch_timeout`,
  e.g., 5 min on continuous mode; immediate on regression-cron mode)
- **Modify** swarm composition before dispatch
- **Override** entirely with a custom swarm spec for this campaign

Operator-mode flag in `config/orchestrator.yaml`:

```yaml
swarm_approval_mode: auto              # auto | review_recommended | review_all
```

- `auto` — dispatch with recommendation immediately; operator
  retrospectively audits via dashboard. Right default for continuous
  testing.
- `review_recommended` — dispatch silently when recommendation matches
  the default; pause for approval when Orchestrator suggests an
  override.
- `review_all` — every campaign waits for operator approval. Right
  default for evidence-gathering runs ahead of a CISO presentation.

---

## 8. Implementation pattern (pure Python, no LLM)

A single module: `app/agents/orchestrator/`. Layout:

```
app/agents/orchestrator/
├── __init__.py                # exports ORCHESTRATOR_VERSION
├── dispatch.py                # entrypoint for orchestrator_dispatch node
├── postverdict.py             # entrypoint for orchestrator_postverdict node
├── scoring.py                 # signal computation + weighted sum
├── constraints.py             # hard-constraint checks
├── budget.py                  # per-category pool + per-campaign sizing
├── swarm_recommender.py       # SwarmRecommendation logic
├── coverage_query.py          # the SQL behind the heat-map state
└── types.py                   # CampaignBrief, SwarmRecommendation, etc.
```

All functions are deterministic (no LLM calls, no `random()` outside
of seeded tie-breakers, no time-of-day side effects). Same Postgres
state → same campaign every time. This is what makes campaign
selection auditable in the same way the `synthesize_fn` is.

---

## 9. Operational defaults

| Concern | Default |
|---|---|
| Dispatch trigger sources | (1) `regression_schedule` due rows (Postgres-driven cron); (2) `/healthz` SHA poller (every 60s in production, every 5min in MVP); (3) manual button in dashboard |
| Idempotency | A `campaigns` row is created per dispatch with a generated `campaign_id`. Re-running with the same state input yields the same `campaign_id` (computed deterministically from `target_version + state_hash + dispatch_timestamp_bucket`) so duplicate triggers don't double-spend |
| Retry | If a Postgres read fails, retry 3× with backoff. If still failing, the dispatch node errors out — no fallback campaign (better to skip a tick than dispatch a bad one) |
| Failure mode | A failed dispatch logs to Langfuse + dashboard "dispatch failures" tile. The next scheduled tick re-attempts |
| Latency | p95 dispatch decision <500ms (it's a few SQL queries + arithmetic) |
| Cost | <$0.001 per dispatch (the LLM-free path means cost is just compute + queries) |

---

## 10. Versioning

`app/agents/orchestrator/__init__.py`:

```python
ORCHESTRATOR_VERSION = "0.4.2"
SCORING_WEIGHTS_VERSION = "0.4.0"
```

Both are recorded in each `campaigns` row. Bumped manually with the
same PR-review discipline as Judge's `RUBRIC_VERSION`. The split
between `ORCHESTRATOR_VERSION` (code logic) and
`SCORING_WEIGHTS_VERSION` (config values) lets you correlate
"did this campaign decision change because we tweaked weights, or
because we changed the algorithm?"

---

## 11. Implementation pointers

To be expanded during the implementation plan:

- LangGraph nodes: `app/graph/nodes/orchestrator_dispatch.py`,
  `app/graph/nodes/orchestrator_postverdict.py`
- Scoring math: `app/agents/orchestrator/scoring.py`
- Hard-constraint checks: `app/agents/orchestrator/constraints.py`
- Budget calculator: `app/agents/orchestrator/budget.py`
- Swarm recommender: `app/agents/orchestrator/swarm_recommender.py`
- Tests:
  - Scoring math truth tables (5 signals × known input states)
  - Hard-constraint precedence (regression_overdue beats high-score cell)
  - Budget caps (pool exhaustion halts category)
  - Swarm recommender produces valid `SwarmRecommendation` for every category in `config/swarm.yaml`
  - Deterministic replay: same state → same `CampaignBrief.campaign_id`
