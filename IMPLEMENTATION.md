# AgentForge Adversarial — Implementation Status

**Submission date:** 2026-05-12 (MVP) · revised 2026-05-13 (operator console complete + auto-auth + multi-target + LangGraph + FAIL/PARTIAL fan-out + swarm picker + Approve gate + parallel LLM) · 2026-05-14 (P2: dashboard tabs refactor + cost rollup + regression harness + history-aware prompts)
**MVP commit:** `513ee25` (initial MVP). Subsequent work on 2026-05-12/13 added: operator-console Launch panel, auto-session-creation auth, multi-target generic factory, LangGraph state machine, and class-probe FAIL fan-out.
**Deployed dashboard:** https://agentforge-adversarial-production.up.railway.app/
**Target under test:** https://copilot-production-b532.up.railway.app/

---

## Scope honesty

This codebase is an **architecture-aligned vertical slice** of
`ARCHITECTURE.md`, not the full multi-agent platform that document
describes. What ships:

- LangGraph 7-node state machine + FAIL/PARTIAL fan-out
- Postgres `campaigns` / `attack_queue` / `attack_runs` two-phase write
  with atomic CHECK constraint
- Ensemble Judge (keyword + LLM, every category)
- Documentation Agent + vulnerability lifecycle (`discovered → triaged
  → fix_proposed → fix_validated / reopened / closed`)
- Multi-target factory (`copilot` / `generic_chat` / `openai_compat`)
- Per-campaign cost rollup (P2)
- Slim regression harness (CLI + Vuln Board button)
- History-aware mutator + class-probe prompts (rounds 1+)

What is **not** in this slice — final-work scope:

- Orchestrator per-cell scoring engine (`docs/components/orchestrator-scoring.md`)
- `synthesize_fn` pipeline that promotes high-novelty FAILs into seed cases
- `regression_schedule` table + cron / per-deploy triggers (the shipped
  regression harness is operator-initiated, not autonomous)
- Langfuse trace integration (needs external account; designed in
  `docs/components/observability.md`)
- Multimodal & Document Poisoning category (needs target-side
  `/v1/documents/attach`)
- pgvector novelty dedup (not planned until seed corpus > 50)

The shipped surface is enough to demonstrate the contract end-to-end
(seed → mutator → dispatch → Judge → vuln → triage → replay) on a real
deployed target. The deferred surface is what would turn it from a
slice into an always-on platform.

---

## TL;DR

A vertical slice of the platform: **8 of 9 designed attack categories**,
**32 seed cases** (8 hand-curated clinical-specific + 15 Garak-derived
[NVIDIA] + 5 JailbreakBench-derived + 4 HouYi indirect-injection patterns
[Liu et al. 2023]) **amplified by an LLM Red Team swarm**
(`mutator` = 3 variants per seed; `class_probe` = 10 boundary variants per
FAIL; `partial_reentry` = 3 fresh phrasings per PARTIAL), dispatched
against any registered AI target via a `targets` table (Co-Pilot,
generic_chat, or openai_compat), verdicts written by an **ensemble Judge
(deterministic keyword + gpt-4o-mini LLM)** to **Railway-hosted Postgres**,
surfaced on a **Streamlit dashboard** with an in-browser Launch panel,
Add-target form, swarm-config picker, Approve/Modify/Override gate, Cancel
button, and phase-aware progress fragment. Control flow runs as a
**LangGraph 7-node state machine** in `agentforge_adversarial/graph.py`
with two conditional edges:
  - `judge → partial_reentry / class_probe / END` (routes by PARTIAL or FAIL count + round budget)
  - `partial_reentry → class_probe / bump_round`

All LLM subagent calls run in parallel under `asyncio.Semaphore(8)`.

| Submission gate | Status |
|---|---|
| Hard gate: 3+ attack categories | ✅ 8 categories shipped |
| Hard gate: agent prototype running live against deployed target | ✅ Red Team mutator + Class-Probe subagent + Ensemble Judge, hits live Co-Pilot OR any registered target |
| Hard gate: working test suite | ✅ 45 tests pass (`make test`) |
| Hard gate: results visible to reviewer | ✅ public dashboard reads from Railway Postgres |

---

## What shipped — component-by-component matrix

Mapped to the section numbers in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Agents (`ARCHITECTURE.md` §2)

| Agent | MVP | Notes |
|---|---|---|
| **Red Team Swarm** | ⚠️ Partial | 2 subagents × gpt-4o-mini, hard-coded models: `red-team-mutator-0` (3 mutations per seed in the mutate node) + `red-team-class-probe-0` (10 boundary variants per FAIL in the class-probe node). **Missing for final:** configurable swarm from `config/swarm.yaml`, multi-LLM (claude-haiku, deepseek, llama-3 via Ollama), parallel synthesis with `synthesize_fn` (designed in `docs/components/synthesis-pipeline.md`). |
| **Judge** | ✅ Shipped | Ensemble: keyword predicates + gpt-4o-mini LLM on every category. Files: `agentforge_adversarial/judges/{keyword,llm_judge,ensemble}.py`. Atomic UPDATE enforced by `attack_runs_judge_atomic` CHECK constraint. |
| **Orchestrator** | ❌ Deferred | Designed in `docs/agents/orchestrator.md` (5-signal weighted scoring, per-category daily pools, score-weighted budget, `SwarmRecommendation`). MVP runner.py uses fixed config + flat seeds + uniform mutator instead. |
| **Documentation Agent** | ✅ Shipped 2026-05-13 | `agentforge_adversarial/documentation_agent.py`. On every FAIL emitted by `judge_node`, writes one `vulnerabilities` row (state=discovered, severity per clinical-safety-weighted category map, parent_vuln_id resolved by walking attack_runs lineage) + one `vuln_reports` row (severity rationale, observed-vs-expected, repro steps, suggested fix, defense reference). Idempotent on `attack_run_id` via UNIQUE constraint. |

### Components (`ARCHITECTURE.md` §4-§7)

| Component | MVP | Notes |
|---|---|---|
| **Attack Queue** | ⚠️ Partial | `attack_queue` table shipped with all designed columns + per-source provenance rules. Dispatch loop is a single sequential for-loop in `runner.py` (not the async `dispatcher_loop` with rate/cost filters from `docs/components/attack-queue.md` §4). |
| **Dispatcher → Judge two-phase write** | ✅ Shipped | Dispatcher INSERTs `attack_runs` with Judge cols NULL; Judge UPDATEs atomically (`attack_runs_judge_atomic` CHECK). Idempotency via `attack_runs.queue_entry_id UNIQUE` + DEFERRABLE FK. |
| **Synthesis pipeline** | ❌ Deferred | Designed in `docs/components/synthesis-pipeline.md` (6-stage: normalize → embed → dedup → novelty → score → budget-cap). MVP enqueues all mutator outputs directly. |
| **Class-probe** | ✅ Shipped 2026-05-13 | FAIL → 10 boundary variants fan-out. Implemented as `class_probe_node` in `graph.py` + `red_team/class_probe.py` (gpt-4o-mini with a boundary-axis system prompt). Lineage stored in `attack_runs.parent_id` (migration 003). Bounded by `--max-rounds` (default 2). |
| **Regression Harness** | ❌ Deferred | Designed in `docs/components/regression-harness.md`. Needs a scheduler; current model is one-shot CLI campaigns. |
| **Dashboard** | ✅ Shipped | Streamlit single-page: KPI cards, altair category × verdict heat-map, filterable run table, per-run drill-down. File: `dashboard/app.py`. Deployed on Railway via `railway.toml` (Nixpacks build, `/healthz` healthcheck). |
| **Observability** | ⚠️ Partial | Stdout logging from `runner.py` only. **Missing for final:** Langfuse traces (one trace per campaign with generation spans), per-attack `langfuse_trace_id` column populated. |
| **Postgres schema** | ⚠️ Partial | 3 of 9 designed tables (`campaigns`, `attack_queue`, `attack_runs`). Subset of `docs/components/database-schema.md` §1-§8. **Missing for final:** `vulnerabilities`, `vuln_reports`, `near_misses`, `cross_regressions`, `threat_model_cells`, `cost_rollup_daily` (view). |
| **pgvector novelty filter** | ❌ Deferred | Designed for HNSW dedup on embeddings. Not used; mutator produces N variants without deduplication. |
| **Operator console (in-dashboard campaign launch)** | ✅ Shipped 2026-05-13 | Launch panel with target picker, "+ Add target" form, swarm-config picker (mutator toggle / class-probe rounds slider / model dropdown), Approve/Modify/Override gate (Review-before-dispatch checkbox), Cancel button, phase-aware progress fragment. Per `docs/components/dashboard.md §4.1`. **Still deferred for final:** Vuln Board (P1), Cost tile (P2). |

### Orchestration framework (`ARCHITECTURE.md` §3, §4)

| Item | MVP | Notes |
|---|---|---|
| **LangGraph state machine** | ✅ Shipped 2026-05-13 | 7-node graph in `agentforge_adversarial/graph.py`: `load_seeds → mutate → dispatch → judge → (decide_after_judge) → partial_reentry / class_probe / END`, with `partial_reentry → (decide) → class_probe / bump_round` and both `class_probe / bump_round → dispatch` (loop). `CampaignState` TypedDict carries pending QueueEntries, dispatched runs, FAIL + PARTIAL collections per round, and round number. Mutator + class-probe + partial-reentry all run their OpenAI calls in parallel under `asyncio.Semaphore(8)`. `runner.py` is now a thin wrapper that resolves the target, builds the chat client, and invokes the compiled graph. |
| **Class-probe fan-out** | ✅ Shipped 2026-05-13 | `agentforge_adversarial/red_team/class_probe.py` — on FAIL, gpt-4o-mini generates up to 10 boundary variants per failing attack (different phrasing / framing / authority axes). Variants enqueued with `source='class_probe'`, `parent_id` = the failing run's id, `round_num` one greater than the parent's. Lineage tracked in `attack_runs.parent_id` (migration `003_lineage.sql`). `max_rounds` bounds the loop (default 2, CLI `--max-rounds N`). |

### Attack categories (`THREAT_MODEL.md` §1-§9)

| # | Code | Category | MVP | Notes |
|---|---|---|---|---|
| 1 | `PI` | Prompt Injection | ✅ | `evals/cases/prompt_injection_persona_hijack.yaml` + 8 Garak-derived (`evals/cases/garak/`) + 5 JailbreakBench (`evals/cases/jailbreakbench/`) + 1 HouYi (`evals/cases/houyi/`) |
| 2 | `DE` | Data Exfiltration | ✅ | `evals/cases/data_exfiltration_cross_patient.yaml` |
| 3 | `SC` | State Corruption | ✅ | `evals/cases/state_corruption_history_poison.yaml` |
| 4 | `TM` | Tool Misuse | ✅ | `evals/cases/tool_misuse_parameter_tampering.yaml` |
| 5 | `DC` | DoS & Cost | ✅ | `evals/cases/dos_cost_repeat_amplification.yaml` — Judge uses a length predicate (`output > 2000 chars`) plus keyword markers |
| 6 | `IR` | Identity & Role | ✅ | `evals/cases/identity_role_panel_bypass.yaml` |
| 7 | `VB` ⭐ | Verification-Gate Bypass | ✅ | `evals/cases/verification_bypass_skip_citation.yaml` — Co-Pilot-specific |
| 8 | `MP` ⭐ | Multimodal & Document Poisoning | ❌ Deferred | Needs `/v1/documents/attach` channel, not `/v1/chat`. Out of scope for the text-channel MVP. |
| 9 | `OL` ⭐ | Observability Leak | ✅ | `evals/cases/observability_leak_system_prompt.yaml` — Co-Pilot-specific |

⭐ = Co-Pilot-specific category (not generic LLM attack pattern).

---

## Live verification (2026-05-12)

Last live campaign against the deployed Co-Pilot (`513ee25`, target_version
`live-2026-05-12-improved-judge`):

| Metric | Value |
|---|---|
| Seeds dispatched | 8 (one per category) |
| LLM-mutated variants | 24 (3 per seed via gpt-4o-mini) |
| Total attack runs | **32** |
| Wall time | ~6 minutes |
| OpenAI cost | ~$0.01 (8 mutator calls + 32 LLM Judge calls) |
| Verdict distribution | **32 PASS · 0 PARTIAL · 0 FAIL** |
| Ensemble Judge disagreements | 3 (all SC mutations: keyword=PARTIAL → LLM=PASS, LLM recognized "no prior conversation" refusals) |
| Test suite | 28/28 passing (`make test`) |

**Reading the all-PASS result honestly:** the deployed Co-Pilot's defenses
held across all 8 categories. This is the truth, not a Judge weakness — the
test suite (`tests/test_keyword_judge.py`) proves the Judge catches FAIL
cases on a deliberately-vulnerable mock target. The platform discriminates
correctly: validates defenses when they hold, catches vulnerabilities when
they exist.

---

## Gap to final submission

Prioritized by demo-credibility-per-hour, with effort estimates. Final
submission should aim for everything in P0 + P1; P2 is bonus.

### P0 — ✅ All shipped 2026-05-13

| Item | Effort | What it unlocks |
|---|---|---|
| ~~LangGraph node/edge skeleton~~ | ✅ ~~3-4 h~~ | 7-node graph in `agentforge_adversarial/graph.py` (load_seeds → mutate → dispatch → judge → partial_reentry / class_probe / bump_round → dispatch loop). |
| ~~In-dashboard Launch button + live progress~~ | ✅ ~~6-7 h~~ | Streamlit Launch panel + subprocess launcher + `st.fragment(run_every=5)` for live progress without full-page reloads. |
| ~~Multi-target picker + add-target form~~ | ✅ ~~3 h~~ | Platform attacks any AI system via the `targets` table (copilot, generic_chat, openai_compat). |
| ~~Conditional edge: FAIL → Class-probe fan-out (10 boundary variants)~~ | ✅ ~~4 h~~ | `decide_after_judge` routes to `class_probe_node` on FAIL && round_num < max_rounds. |
| ~~Conditional edge: PARTIAL → Mutator re-entry~~ | ✅ ~~2 h~~ | `partial_reentry_node` re-mutates ambiguous attacks (3 fresh phrasings per PARTIAL); chains into `class_probe` if FAILs also exist in the same judge pass. |
| ~~Swarm-config picker on Launch panel~~ | ✅ ~~1.5 h~~ | Mutator on/off, Class-probe rounds slider (0–3), Mutator model dropdown (gpt-4o-mini / gpt-4o / gpt-4.1-mini) — passed via `--max-rounds` CLI flag + `MUTATOR_MODEL` / `JUDGE_MODEL` env vars to the subprocess. |
| ~~Approve/Modify/Override gate UI~~ | ✅ ~~2 h~~ | "Review before dispatch" checkbox in Launch panel: when on, clicking Run shows the proposed swarm spec + 3 buttons (Accept dispatches as-is, Modify returns to the picker, Override accepts JSON paste). Per `docs/components/dashboard.md §4.1`. |
| ~~Parallel mutator + class-probe (asyncio.gather + Semaphore)~~ | ✅ ~~0.5 h~~ | Reduces 8-seed mutator wall-time from ~30 s → ~5 s; class-probe with N FAILs goes from sequential to bounded-parallel (concurrency=8). |
| ~~Phase-aware progress text~~ | ✅ ~~0.5 h~~ | Dashboard fragment tails the subprocess log file (`var/campaigns/*.log`) and surfaces the latest `[graph:*]` phase line so the operator sees "Mutator generating…" instead of "0 attacks landed" during the startup window. |

### P1 — ✅ All shipped 2026-05-13

| Item | Effort | What it unlocks |
|---|---|---|
| ~~`vulnerabilities` + `vuln_reports` tables~~ | ✅ ~~2 h~~ | `migrations/004_vuln_lifecycle.sql`: `vulnerabilities` (state machine: discovered/triaged/fix_proposed/fix_validated/reopened/closed; parent_vuln_id for class-probe lineage; severity ENUM weighted by clinical-safety) + `vuln_reports` (severity_rationale, observed_vs_expected, repro_steps, suggested_fix, defense_reference). |
| ~~Documentation Agent~~ | ✅ ~~3 h~~ | `agentforge_adversarial/documentation_agent.py`. On every FAIL in `judge_node` writes one vulnerabilities row + one vuln_reports row. Idempotent on `attack_run_id`. Lineage resolution walks `attack_runs.parent_id` chain to link class-probe variants back to their root vulnerability. |
| ~~Vulnerability board on dashboard~~ | ✅ ~~2 h~~ | `dashboard/app.py` "🛡️ Vulnerability Board" section between heat-map and run-table. KPI cards (Total / Critical / High / Discovered / Triaged), filterable table, drill-down to severity rationale + observed-vs-expected + repro + suggested fix + defense reference. State-transition buttons (→ Triage / → Fix proposed / → Close) per `docs/components/dashboard.md §4.2`. |
| ~~Seed corpus expansion via attack-library imports~~ | ✅ ~~3 h~~ | 8 hand-curated seeds + 15 Garak-derived (`evals/cases/garak/`, NVIDIA's red-team toolkit) + 5 JailbreakBench (DAN/AIM/Developer/Hypothetical/Translate-trick) + 4 HouYi indirect-injection patterns (Liu et al. 2023). Total: **32 seeds across 32 subcategories** spanning 8 categories. `cases.py:load_cases()` recurses through subdirectories. |

### P2 — observability + cost rollup + regression

| Item | Effort | What it unlocks |
|---|---|---|
| Langfuse integration (`langfuse_trace_id` per campaign, generation spans per attack) | 3 h | Designed in `docs/components/observability.md`. Gives reviewers a trace per attack. **Status: deferred** — requires an external Langfuse account; out of scope for tonight's submission. |
| ~~Per-campaign LLM cost rollup~~ | ✅ ~~1.5 h~~ | Shipped 2026-05-14 as migration `005_cost_rollup.sql` + `agentforge_adversarial/cost.py` + sidebar "Red-team LLM cost" tile. ContextVar threads `campaign_id` through `asyncio.gather`; per-1M-token USD price table (2026-05 snapshot) for gpt-4o, gpt-4o-mini, gpt-4.1-mini. Cost is harness-side only — target's own LLM bill is not visible through a black-box chat interface. |
| ~~Regression Harness — slim version~~ | ✅ ~~2 h~~ | Shipped 2026-05-14 as `agentforge_adversarial/regression.py` + CLI `python -m agentforge_adversarial regress` + Vuln-Board "🔁 Replay" button. Replay verdict → state map: PASS → `fix_validated`, FAIL → `reopened`, PARTIAL → no change. Replays use the same ensemble Judge as the live graph. **Deferred from slim:** `regression_schedule` table + cron triggers + `attack_queue` `source='regression'` enqueue. |
| ~~History-aware mutator + class-probe prompts (D)~~ | ✅ ~~1.5 h~~ | Shipped 2026-05-14 as `red_team/mutator.py:_format_history_hint()`. In rounds 1+, the LLM sees a same-category-filtered list of REFUSED (= PASS) and SUCCEEDED (= FAIL) prompts. Biases variants away from already-defended framings. |
| Multimodal & Document Poisoning (MP) — 9th attack category via `/v1/documents/attach` | 4 h | Per `THREAT_MODEL.md` §8. **Status: deferred** — requires target-side `/v1/documents/attach` endpoint + multipart dispatcher. Out of scope for tonight; 8/9 categories shipped is the demo claim. |

### Not planned for final

- pgvector novelty filter (HNSW dedup on attack embeddings) — only valuable
  once seed pool grows beyond ~50 cases.
- Ollama / OSS-LLM swarm — Co-Pilot rate limits + OpenAI-only judge already
  cover the demo story; OSS-LLM swarm is a research direction not a
  submission requirement.
- Front-desk role attack vectors — Co-Pilot has the role boundary in `B6`
  panel-scope gate; MVP's IR category covers it generically.

---

## File map — where the work actually lives

| Path | Purpose |
|---|---|
| `agentforge_adversarial/runner.py` | Thin wrapper that resolves the target, builds the chat client, and invokes the compiled LangGraph state machine. |
| `agentforge_adversarial/graph.py` | LangGraph `CampaignState` + 5 nodes (load_seeds, mutate, dispatch, judge, class_probe) + `decide_after_judge` conditional edge. |
| `agentforge_adversarial/red_team/class_probe.py` | Class-probe subagent (10 boundary variants per FAIL via gpt-4o-mini). |
| `agentforge_adversarial/targets.py` | CRUD helpers over the `targets` table (list, get-by-name, default, add). |
| `agentforge_adversarial/queue.py` | `create_campaign`, `enqueue_cases` (writes to `attack_queue`). |
| `agentforge_adversarial/target.py` | `CopilotClient` (session-reuse against live target), `MockCopilotClient` (vulnerable test double), `dispatch_to_attack_run`, `insert_attack_run`. |
| `agentforge_adversarial/judges/keyword.py` | Deterministic Judge — 8 category-specific marker sets + length predicate for DC. |
| `agentforge_adversarial/judges/llm_judge.py` | gpt-4o-mini Judge — category-general SYSTEM_PROMPT, takes `expected_failure_mode` from seed as the per-category rubric. |
| `agentforge_adversarial/judges/ensemble.py` | Combines keyword + LLM (every category), UPDATEs `attack_runs` atomically. |
| `agentforge_adversarial/red_team/mutator.py` | One Red Team subagent — gpt-4o-mini, produces 3 mutations per seed. |
| `agentforge_adversarial/target.py` | `CopilotClient` (auto-creates session via `POST /v1/sessions`, 404-retry) + `MockCopilotClient` (deliberately-vulnerable test double). |
| `dashboard/app.py` | Streamlit dashboard, reads Postgres via `psycopg`. Includes the Launch panel (`§4.1` partial). |
| `dashboard/launcher.py` | Subprocess-based campaign runner used by the Launch panel. |
| `migrations/001_initial.sql` | 3 tables + atomic CHECK + DEFERRABLE FK. Strict subset of full schema design. |
| `evals/cases/*.yaml` | 8 seed test cases (1 per shipped category). |
| `tests/` | 30 passing tests — unit + live-Postgres integration. |

---

## Operating the platform

Per `README.md` "Setup" + "Run a campaign" sections. The submission-ready
flow has two surfaces:

**From the browser (preferred):**

1. Open https://agentforge-adversarial-production.up.railway.app/
2. Launch panel → choose **Live** → click **▶ Run campaign**
3. Progress bar fills; heat-map auto-refreshes on completion.

**From the CLI:**

```bash
export COPILOT_PATIENT_ID=<synthea-patient-uuid>
DATABASE_URL=<railway-url> make run-live
# refresh https://agentforge-adversarial-production.up.railway.app/
```
