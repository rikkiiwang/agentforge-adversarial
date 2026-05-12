# Architecture — AgentForge Adversarial AI Security Platform

**Status:** Design-of-record, draft 2026-05-11. Per-block design docs in
`docs/agents/`, `docs/components/`, `docs/taxonomy/`.

---

## Summary (~500 words)

The platform exists to answer one question continuously: *is the OpenEMR
Clinical Co-Pilot — the W1/W2 deliverable — getting more or less resilient
over time against adversarial pressure?* A static test suite cannot answer
that. The system must hunt, mutate, evaluate, and document vulnerabilities
without a human in the loop for every step, then prove that fixes hold
against future variants.

The architecture is a **four-agent multi-agent system** orchestrated by
LangGraph, persisted in Postgres, observed via Langfuse, and surfaced to
humans through a three-view dashboard. The four agents are:

1. **Orchestrator Agent** — reads a coverage heat-map and decides what to
   probe next. It picks a campaign cell `(category, subcategory, channel)`,
   sets a cost budget, and configures the Red Team swarm (how many
   subagents, which models). After the swarm fans out, the Orchestrator is
   re-invoked to route post-verdict signals (FAIL → docs / regression /
   class-probe; PARTIAL → mutator).

2. **Red Team Swarm** — a configurable parallel fleet of *N* subagents,
   each with its own LLM (typically a local, ablated, or open-source model
   chosen for offensive workflows; frontier models often refuse).
   Subagents independently generate candidate attacks, each structurally
   declaring `(category, subcategory, channel, technique, expected_failure_mode)`.

3. **Judge Agent** — independent evaluator powered by a lightweight LLM
   (Haiku 4.5 by default). For every attack run, the Judge emits one of
   three verdicts (`PASS` / `PARTIAL` / `FAIL`) and validates that the
   declared category matches what actually happened. Different model from
   Red Team subagents by policy: an agent that both generates and judges
   attacks has a conflict of interest by design.

4. **Documentation Agent** — converts confirmed exploits into structured
   vulnerability reports (severity, repro, observed-vs-expected, suggested
   remediation). High-severity reports require human approval before they
   leave the dashboard.

The fan-in step from the Red Team swarm is **plain Python, not an LLM** —
`synthesize_fn()` does embedding-based dedup, historical novelty filtering,
weighted scoring, and budget-capped greedy selection with subagent and
channel diversity floors. Mirrors the W2 precedent that the supervisor is
plain Python (W2_ARCHITECTURE §4.1). Cheap, deterministic, replayable.

Post-attack, the Judge writes to **Postgres** — the single source of truth
across six tables (`attack_runs`, `vulnerabilities`, `vuln_reports`,
`near_misses`, `regression_schedule`, `threat_model_cells`). The dashboard
renders three views on top: a coverage heat-map (rows = categories,
columns = subcategories), a near-miss tile (active partials under
exploration), and a vulnerability board with a state machine
(`discovered → triaged → fix_proposed → fix_validated → closed`, with
`reopened` on regression). A weekly cron triggers the regression replay
queue to detect when a previously-fixed vulnerability has reappeared.

Trust boundaries are explicit. The Red Team swarm has the lowest trust —
its outputs are attacks, never auto-applied to the target. The Judge's
verdicts are authoritative but auditable. The Orchestrator's campaign
decisions are budget-capped. The Documentation Agent's high-severity
reports route through human review before external filing. Suggested fixes
are never auto-applied.

The threat taxonomy combines the six categories the Week 3 spec mandates
(prompt injection, data exfiltration, state corruption, tool misuse, DoS,
identity / role) with three Co-Pilot-specific extensions chosen for
**defense-mapped probing**: verification-gate bypass (W1's Layer-1
attribution + Layer-2 rules), multimodal poisoning (W2's VLM ingestion
pipeline), and observability leak (PHI in traces / audit logs). Every
category cross-references OWASP LLM Top 10 + MITRE ATLAS identifiers.

---

## 1. Target

The platform attacks the **deployed Clinical Co-Pilot** — the FastAPI
service at `https://copilot-production-b532.up.railway.app/` — exactly as
an external client would, via HTTPS. No backdoors, no privileged hooks.

The target version is tracked by **git SHA** read from the Co-Pilot's
`/healthz` endpoint. Every `attack_run` row records the target version,
so coverage metrics are version-scoped. Regression replays compare
verdicts across consecutive versions.

Sub-systems exposed to the platform:

- `POST /v1/sessions` — session open (panel-scope gate fires here)
- `POST /v1/chat` — main chat turn
- `POST /v1/documents/attach` — multimodal ingestion (W2)
- `POST /v1/documents/{id}/confirm` — front-desk → physician handoff (W2)
- `GET /v1/sessions/{id}/pending_intakes` — banner content (W2)

The platform never touches OpenEMR's FHIR or DB directly; everything goes
through the Co-Pilot.

---

## 2. Agent roster

| Agent | Responsibility | Trust level | Default model |
|---|---|---|---|
| Orchestrator | Campaign selection · swarm configuration · post-verdict signal routing | Medium — decides spend, budget-capped | Haiku 4.5 (or pure Python config table for MVP) |
| Red Team Swarm | Novel attack generation · multi-turn sequences · mutation / variants | Lowest — outputs never auto-applied | User-configurable; default mix of local OSS (Llama 3.x / Mistral) + ablated models via Ollama |
| Judge | Verdict (PASS/PARTIAL/FAIL) · category-claim validation | Medium — verdicts authoritative but auditable | Haiku 4.5 |
| Documentation | Reads FAIL signal · assigns severity (per-category baseline + LLM modifier) · writes `vulnerabilities` + `vuln_reports` rows in `discovered` state · updates report when Class-probe completes · suggests defense-mapped remediation. Detail: `docs/agents/documentation-agent.md`. | Medium — high-severity human-gated | Sonnet 4.6 |

The Judge and Red Team Swarm **must use different model families**.
Co-incidence of model family between the attacker and judge is a known
conflict of interest in adversarial evaluation.

---

## 3. Diagram

```
                            ┌────────────────────────┐
   ┌── new target deploy ──▶│  ORCHESTRATOR AGENT    │◀── coverage heat-map
   │       (cron / hook)    │  (LangGraph: dispatch) │     near-miss tile
   │                        └───────────┬────────────┘     vuln board
   │                                    │ campaign brief
   │                                    ▼
   │            ┌─────────────────────────────────────────────────┐
   │            │           RED TEAM SWARM (parallel fan-out)     │
   │            │  subagent #1   subagent #2   subagent #3  ...   │
   │            │  (Llama-3-ab)  (DeepSeek)    (Mistral-7B)       │
   │            │                                                 │
   │            │  → N×M candidate attacks (structured output)    │
   │            └─────────────────────┬───────────────────────────┘
   │                                  │
   │                                  ▼
   │               ┌───────────────────────────────────┐
   │               │  synthesize_fn()  (plain Python)  │
   │               │  normalize → embed → dedup →      │
   │               │  novelty → score → budget-cap     │
   │               └─────────────────┬─────────────────┘
   │                                 │ K final attacks
   │                                 ▼
   │                  ┌──────────────────────────┐
   │                  │  TARGET                  │
   │                  │  Co-Pilot (live, by SHA) │
   │                  └──────────────┬───────────┘
   │                                 │ transcript + outcome
   │                                 ▼
   │                  ┌──────────────────────────┐
   │                  │  JUDGE AGENT (lightweight LLM) │
   │                  │  PASS / PARTIAL / FAIL          │
   │                  │  + category-validation          │
   │                  └─┬───────────┬───────────┬──────┘
   │                    │           │           │
   │                  PASS        PARTIAL      FAIL
   │                    │           │           │
   │                    ▼           ▼           ▼
   │             coverage    near_misses   [3-way fan-out]
   │              ledger     (mutator   ┌────┬─────────┬──────┐
   │                          loop)     ▼    ▼         ▼      ▼
   │                                  Doc  Regression Class-probe
   │                                  Agt   harness   (red team
   │                                                   mutator)
   │
   │              ┌────────────────────────────────────────────┐
   └─────────────│   ORCHESTRATOR (re-invoked, post-verdict)   │
                  │   reads new state · feeds next campaign     │
                  └────────────────────────────────────────────┘
                                  │
              ┌───────────────────┴───────────────────────┐
              ▼                                           ▼
      ┌──────────────────┐                        ┌──────────────────┐
      │  POSTGRES        │                        │  LANGFUSE        │
      │  (6 tables)      │                        │  (traces, costs) │
      └─────────┬────────┘                        └──────────────────┘
                │
                ▼
      ┌──────────────────────────────────────────────┐
      │  DASHBOARD (Human-in-Loop)                   │
      │  · coverage heat-map  · near-miss tile       │
      │  · vuln board (state machine)  · cost tile   │
      │  · approval gates                            │
      └──────────────────────────────────────────────┘
```

A rendered version of this diagram will live under `docs/diagrams/`.

---

## 4. Control flow — one campaign end-to-end

1. **Trigger.** New target version deployed (`/healthz` SHA change), or
   scheduled tick (weekly), or human kick-off via dashboard.
2. **Orchestrator dispatch.** Reads coverage heat-map. Identifies the
   highest-leverage cell (untested → wavering → exploited-but-unrepaired).
   Sets cost budget. Picks Red Team swarm configuration (N subagents,
   per-subagent model from config table). Emits campaign brief.
3. **Red Team fan-out.** LangGraph spawns *N* subagent nodes in parallel.
   Each generates M candidate attacks under the brief, emitting structured
   output. The swarm produces N×M candidates.
4. **Synthesis (plain Python).** `synthesize_fn()` runs six stages:
   normalize → embed → within-batch dedup → novelty filter against
   `attack_runs` history → weighted score → budget-capped greedy pick with
   subagent + channel diversity floors. Output: K final attacks
   (`K ≤ N×M`, typically `K ≈ 4–10`).
5. **Dispatch to target.** Each final attack is executed against the live
   Co-Pilot via HTTPS. Transcript captured. Cost recorded.
6. **Judge.** Independent LLM evaluates the transcript per category-specific
   boolean rubric. Emits `PASS / PARTIAL / FAIL` + `category_validated:
   bool`. Writes one `attack_runs` row per execution.
7. **Post-verdict routing** (Orchestrator re-invoked):
   - PASS → bump coverage signal, no further work.
   - PARTIAL → spawn `near_misses` row in `exploring` state; mutator
     produces N variants per round, capped at K rounds; reuses
     synthesize_fn each round.
   - FAIL → 3-way parallel fan-out:
     - **(a) Documentation Agent** runs the report pipeline:
       1. Read FAIL `attack_runs` row + lineage (`parent_id` chain) +
          joined `threat_model_cells` row
       2. Assign severity = category baseline ± LLM modifier (per
          `docs/agents/documentation-agent.md` §4)
       3. INSERT `vulnerabilities` row (state=`discovered`) + `vuln_reports`
          row (`class_probe_status=pending`)
       4. When Class-probe completes (out-of-band, ~minutes), UPDATE
          `vuln_reports` with `class_probe_variant_count` and
          `class_probe_affected_channels`. For each variant that itself
          FAILed, recursively INSERT a new `vulnerabilities` row with
          `parent_vuln_id` pointing to this one.
     - **(b) Regression Harness** inserts into `regression_schedule`
       (keyed by the FAIL `attack_runs.id` + originating `vuln_id`).
     - **(c) Class-probe** (Red Team mutator) spawns ~10 variants to map
       the vulnerability boundary; each variant becomes its own
       `attack_runs` row → Judge → (per-variant verdict).
8. **Persist signals.** Postgres tables updated; Langfuse trace closed;
   dashboard views recompute on next query.

---

## 5. Inter-agent communication

Three layers, by lifetime:

| Layer | Lifetime | Backed by | Carries |
|---|---|---|---|
| **In-run state** | One campaign run | LangGraph `StateGraph` typed dict | Campaign brief, swarm outputs, synthesized attacks, transcripts, verdicts |
| **Cross-run history** | Forever | Postgres | All durable rows: `attack_runs`, `vulnerabilities`, `near_misses`, `vuln_reports`, `regression_schedule`, `threat_model_cells` |
| **Observability** | Indefinite | Langfuse Cloud | Per-agent traces, token spans, cost spans, inter-agent message log, replay-by-vuln-id |

Agents do **not** call each other directly. All hand-offs go through the
LangGraph state machine (in-run) or Postgres (cross-run). This makes the
system inspectable, replayable, and resilient to single-agent failures —
a crashed subagent leaves a partial swarm output that `synthesize_fn` can
still operate on.

---

## 6. Persistence — Postgres schema

Six core tables. Full schema with migrations in
`docs/components/database-schema.md`. Sketch:

- **`attack_runs`** — one row per attack execution. The canonical record.
  Columns include `id`, `target_version`, `category`, `subcategory`,
  `channel`, `parent_id`, `seed_id`, `source` (direct / random / mutator /
  regression), `red_team_model`, `judge_verdict`, `judge_reasoning`,
  `category_validated`, `cost_usd`, `latency_ms`, `transcript_uri`,
  `langfuse_trace_id`, `embedding` (pgvector), `created_at`.

- **`vulnerabilities`** — confirmed exploits in the state machine.
  References `attack_runs.id` for the origin. *Written by Documentation
  Agent (initial `discovered` state) and Regression Harness
  (`fix_validated` / `reopened` transitions). All other state transitions
  originate from humans via the dashboard — see §13.*

- **`vuln_reports`** — one-to-one with `vulnerabilities`. Carries the
  human-readable report content + human-review state. *Written and
  updated exclusively by the Documentation Agent. Full column schema in
  `docs/components/database-schema.md`.*

- **`near_misses`** — one row per `PARTIAL`. Tracks variant-exploration
  lifecycle (`exploring → escalated / exhausted / budget_capped`).

- **`regression_schedule`** — weekly replay queue. Stores last verdict +
  target_versions_passed array for trend detection.

- **`threat_model_cells`** — the structured taxonomy. Versioned. Drives
  the heat-map columns + quality bars.

A `pgvector` index on `attack_runs.embedding` enables sub-100ms novelty
filtering against ~100K historical attacks (synthesis stage 4).

---

## 7. Dashboard

Three views, all backed by Postgres queries. No separate caching layer for
MVP; materialized views added when query latency exceeds 1s.

| Tile | Query | Purpose |
|---|---|---|
| **Coverage heat-map** | `GROUP BY category, subcategory, channel, target_version` over `attack_runs` | Cells colored by state (`untested / under-tested / wavering / exploited / hardened`); quality-bar fraction + diversity score shown per cell |
| **Near-Miss tile** | `SELECT … FROM near_misses WHERE state='exploring'` | Active partials being mutated; lineage chains |
| **Vuln board** | `SELECT … FROM vulnerabilities JOIN vuln_reports` | State machine; severity; assignee; approval gates for high-severity reports |

Drill-down has two levels — click a heat-map cell → channel breakdown;
click a channel row → individual cases with lineage and trace links.
Detail in `docs/components/dashboard.md`.

---

## 8. Threat taxonomy — 9 categories

The complete taxonomy lives in `THREAT_MODEL.md`. Categories at top level
(⭐ = Co-Pilot-specific extension):

1. Prompt Injection
2. Data Exfiltration
3. State Corruption
4. Tool Misuse
5. DoS & Cost
6. Identity & Role
7. Verification-Gate Bypass ⭐
8. Multimodal & Document Poisoning ⭐
9. Observability Leak ⭐

Each category file under `docs/taxonomy/` defines its subcategories,
quality bars (default 5 channels or category-native dims), Co-Pilot
defenses being probed, seed attack examples, and OWASP / MITRE ATLAS
identifiers.

---

## 9. Trust boundaries

| Action | Trust | Mechanism |
|---|---|---|
| Generate an attack | Low (Red Team) | Attacks live in the Red Team swarm's output stream; never executed unless dispatched by Orchestrator after synthesis. |
| Execute against target | Medium (Orchestrator) | Budget-capped per campaign; per-agent token cap; per-category cost ceiling. |
| Emit verdict | Medium (Judge) | Audit trail in `attack_runs.judge_reasoning`; cross-judge ensemble for high-severity FAILs (Final-scope). |
| File vuln report | Medium (Doc Agent) | Low/Medium severity auto-files in `discovered` state. **High severity gates on human approval** before transitioning to `triaged`. |
| Class-probe boundary update | Medium (Doc Agent) | Async UPDATE of `vuln_reports` after Class-probe completes; bounded by per-vuln `class_probe_cost_cap`; partial results are OK and preserved as `class_probe_status=in_progress`. |
| Reopen a closed vuln | High | Human acknowledgement required before adding to next-campaign rotation. |
| Apply a proposed fix | n/a — never automated | All fixes are *suggestions* the Documentation Agent writes into `vuln_reports.suggested_fix`. Application is out-of-platform. |

Detail in `docs/components/dashboard.md` (approval gates UX) and
`docs/agents/documentation-agent.md` (severity logic).

---

## 10. Cost model

Three layers of caps to prevent the platform from running away in cost:

- **Per-campaign budget** — set by Orchestrator at dispatch time; sum of
  estimated swarm cost + estimated target dispatch cost. Synthesis cap
  enforces this floor.
- **Per-agent token cap** — defensive; prevents a single LLM call from
  consuming the whole budget.
- **Per-category cost ceiling** — Orchestrator deprioritizes a category
  when burn-rate > finding-rate over the trailing window. Hardened
  categories cost-out naturally.

The dashboard's cost tile reports `$/run`, `cost-per-finding`, and
per-agent burn rate. Detail in `docs/components/dashboard.md`.

---

## 11. Known tradeoffs

| Decision | What it buys | What it gives up | Mitigation |
|---|---|---|---|
| Plain-Python synthesis (no LLM) | Cheap, deterministic, replayable, inspectable | Loses semantic dedup across language / encoding boundaries | Optional LLM tiebreaker on top-K only; channel tag is the join key for encoded-vs-plaintext |
| Lightweight Judge LLM | Fast, cheap (Haiku) | May miss subtle FAILs that need deeper reasoning | Cross-judge ensemble (Sonnet + Haiku) for high-severity; periodic ground-truth recalibration |
| Single Postgres (not message queue) | Simple ops; ACID for vuln state | Scaling beyond ~10 campaigns/min becomes write-contention bound | Read replicas; partitioning by target_version; sketched in `docs/components/database-schema.md` |
| Red Team uses local / OSS models | Bypasses frontier refusal of offensive workflows; lower marginal cost | Lower attack quality per turn vs Claude / GPT-4o | Configurable: operator can swap in a frontier model with a red-team-tuned system prompt; documented in `docs/agents/red-team-swarm.md` |
| LangGraph (not CrewAI / AutoGen / custom) | Matches W2 supervisor pattern; team familiarity; deterministic state machine | Less optimized for huge agent populations | Acceptable for 4-agent topology + 10–50 subagent swarms |
| Loose interpretation of spec's "fork from OpenEMR" | Clean conceptual separation; faster iteration | Risk of grader interpreting strict | README clearly links both repos; demo shows the cross-repo flow explicitly |

---

## 12. Per-block design docs

| Block | File |
|---|---|
| Orchestrator Agent | `docs/agents/orchestrator.md` |
| Red Team Swarm | `docs/agents/red-team-swarm.md` |
| Judge Agent | `docs/agents/judge.md` |
| Documentation Agent | `docs/agents/documentation-agent.md` |
| Synthesis pipeline (Python) | `docs/components/synthesis-pipeline.md` |
| Attack queue | `docs/components/attack-queue.md` |
| Regression harness + weekly cron | `docs/components/regression-harness.md` |
| Postgres schema | `docs/components/database-schema.md` |
| Dashboard | `docs/components/dashboard.md` |
| Observability (Langfuse) | `docs/components/observability.md` |
| Threat model (taxonomy index) | `THREAT_MODEL.md` |
| Per-category detail | `docs/taxonomy/01-...md` through `09-...md` |

---

## 13. Vulnerability lifecycle state machine

Every `vulnerabilities` row moves through these states. The table below is
the **only** authority on who can write each transition. The dashboard
enforces this — the UI greys out actions the current actor isn't
authorized to perform.

| State | Writer | Trigger |
|---|---|---|
| `discovered` | Documentation Agent | New FAIL verdict from Judge |
| `triaged` | Human (dashboard) | Operator acknowledges report; reviews observed-vs-expected + suggested fix |
| `fix_proposed` | Human (dashboard) | Operator records a fix plan / commit reference in the dashboard |
| `fix_validated` | Regression Harness | Replay of the originating attack + all linked variants PASS on a later `target_version` |
| `closed` | Human (dashboard) | Operator confirms validation + clinical sign-off |
| `reopened` | Regression Harness | A closed / validated vuln re-FAILs on a later regression replay; loops back into `triaged` |

```
discovered → triaged → fix_proposed → fix_validated → closed
                                           ↑              │
                                           └── reopened ──┘
```

Notes:

- The Documentation Agent **only** writes the `discovered` state on
  initial INSERT. Its subsequent UPDATEs (e.g., Class-probe boundary
  results) modify `vuln_reports` content but never the `state` column.
- The Regression Harness writes `fix_validated` only when **every**
  attack linked by `parent_vuln_id` to a given root vuln passes the
  replay (i.e., variant exploitation also no longer fires). Otherwise the
  status stays `fix_proposed`.
- `reopened` always loops back to `triaged`, not to `discovered`, so the
  operator knows context already exists.
- Detailed UX of the human-driven transitions lives in
  `docs/components/dashboard.md`.

---

## Open questions (to resolve before MVP)

- **Cohere vs local cross-encoder for synthesis re-ranking.** Defaulting
  to local `sentence-transformers` to avoid CI external-API exposure;
  Cohere as an opt-in for production.
- **Judge ensemble triggering threshold.** Always-on for category=
  `verification_bypass` / `multimodal_poisoning` / `observability_leak`
  (the ⭐ categories) given their CISO-defense weight?
- **Regression cadence.** Weekly cron is the default. Should `vuln_status =
  fix_validated` regressions run more frequently (e.g., daily for 30 days)?
- **Cost of `pgvector` similarity at scale.** At 100K rows, index-only
  scans hold sub-100ms. Plan for HNSW migration past 1M attempted attacks.
