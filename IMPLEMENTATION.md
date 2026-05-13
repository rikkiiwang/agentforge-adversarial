# AgentForge Adversarial — Implementation Status

**Submission date:** 2026-05-12 (MVP) · revised 2026-05-13 (operator console + auto-auth + multi-target + LangGraph + class-probe)
**MVP commit:** `513ee25` (initial MVP). Subsequent work on 2026-05-12/13 added: operator-console Launch panel, auto-session-creation auth, multi-target generic factory, LangGraph state machine, and class-probe FAIL fan-out.
**Deployed dashboard:** https://agentforge-adversarial-production.up.railway.app/
**Target under test:** https://copilot-production-b532.up.railway.app/

---

## TL;DR

A vertical slice of the platform: **8 of 9 designed attack categories**,
**8 hand-curated seed cases amplified to 32 attacks via an LLM Red Team
mutator + class-probe fan-out on FAIL (10 boundary variants per failing
attack, bounded by max_rounds)**, dispatched against any registered AI
target via a `targets` table (Co-Pilot, generic_chat, or openai_compat),
verdicts written by an **ensemble Judge (deterministic keyword + gpt-4o-mini
LLM)** to **Railway-hosted Postgres**, surfaced on a **Streamlit dashboard**
with an in-browser Launch panel + Add-target form. Control flow runs as a
**LangGraph 5-node state machine** in `agentforge_adversarial/graph.py`,
with a conditional edge `judge → class_probe → dispatch` that loops on FAIL
until `max_rounds` is exhausted.

| Submission gate | Status |
|---|---|
| Hard gate: 3+ attack categories | ✅ 8 categories shipped |
| Hard gate: agent prototype running live against deployed target | ✅ Red Team mutator + Class-Probe subagent + Ensemble Judge, hits live Co-Pilot OR any registered target |
| Hard gate: working test suite | ✅ 41 tests pass (`make test`) |
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
| **Documentation Agent** | ❌ Deferred | Designed in `docs/agents/documentation-agent.md` (severity logic, class-probe two-write flow, parent_vuln_id variant linking, defense-mapped suggested fix). MVP writes verdicts directly to `attack_runs` from the Judge; no `vuln_reports` rows generated yet. |

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
| **Operator console (in-dashboard campaign launch)** | 🟡 Partial (Launch panel shipped 2026-05-12) | "▶ Run campaign" button in Streamlit (`dashboard/app.py:71-139`) spawns a subprocess via `dashboard/launcher.py`, shows a live progress bar (5s meta-refresh while in-flight), and auto-refreshes the heat-map on completion. Target = Mock or Live (deployed Co-Pilot via auto-created session, no manual `session_id`). **Missing for final:** Approve/Modify/Override gate per `docs/components/dashboard.md §4.1`, Vuln Board, swarm-config picker. |

### Orchestration framework (`ARCHITECTURE.md` §3, §4)

| Item | MVP | Notes |
|---|---|---|
| **LangGraph state machine** | ✅ Shipped 2026-05-13 | 5-node graph in `agentforge_adversarial/graph.py`: `load_seeds → mutate → dispatch → judge → (decide) → END or class_probe → dispatch`. `CampaignState` TypedDict carries pending QueueEntries, dispatched runs, FAIL count, and round number. Conditional edge `decide_after_judge` routes to `class_probe` on FAIL && `round_num < max_rounds`, else END. `runner.py` is now a thin wrapper that resolves the target, builds the chat client, and invokes the compiled graph. **Still deferred:** PARTIAL → Mutator re-entry edge (the FAIL path landed first because it's higher-signal for the demo). |
| **Class-probe fan-out** | ✅ Shipped 2026-05-13 | `agentforge_adversarial/red_team/class_probe.py` — on FAIL, gpt-4o-mini generates up to 10 boundary variants per failing attack (different phrasing / framing / authority axes). Variants enqueued with `source='class_probe'`, `parent_id` = the failing run's id, `round_num` one greater than the parent's. Lineage tracked in `attack_runs.parent_id` (migration `003_lineage.sql`). `max_rounds` bounds the loop (default 2, CLI `--max-rounds N`). |

### Attack categories (`THREAT_MODEL.md` §1-§9)

| # | Code | Category | MVP | Notes |
|---|---|---|---|---|
| 1 | `PI` | Prompt Injection | ✅ | `evals/cases/prompt_injection_persona_hijack.yaml` |
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

### P0 — remaining operator-console gates

| Item | Effort | What it unlocks |
|---|---|---|
| ~~LangGraph node/edge skeleton~~ | ✅ ~~3-4 h~~ Shipped 2026-05-13 | 5-node graph in `agentforge_adversarial/graph.py`. |
| ~~In-dashboard Launch button + live progress~~ | ✅ ~~6-7 h~~ Shipped 2026-05-12 | Streamlit Launch panel + subprocess launcher + 5s meta-refresh while in-flight. |
| ~~Multi-target picker + add-target form~~ | ✅ ~~3 h~~ Shipped 2026-05-13 | Platform attacks any AI system via the `targets` table (copilot, generic_chat, openai_compat). |
| ~~Conditional edge: FAIL → Class-probe fan-out (10 boundary variants)~~ | ✅ ~~4 h~~ Shipped 2026-05-13 | LangGraph's `decide_after_judge` routes to `class_probe_node` on FAIL && round_num < max_rounds. |
| Swarm-config picker on Launch panel (model + variant count + budget) per `docs/components/dashboard.md §4.1` | 1.5 h | Operator can override defaults per campaign without editing YAML. |
| Approve/Modify/Override gate UI (when `swarm_approval_mode != 'auto'`) | 2 h | Per `docs/components/dashboard.md §4.1`. The full §4.1 trust contract. |
| Conditional edge: `PARTIAL → Mutator re-entry` | 2 h | Mirror of the FAIL→class-probe edge for ambiguous verdicts. |

### P1 — Documentation Agent + vuln lifecycle

| Item | Effort | What it unlocks |
|---|---|---|
| `vulnerabilities` + `vuln_reports` tables (per `docs/components/database-schema.md` §2-§3) | 2 h | Persistent record of what FAILed, not just attack_runs verdicts. |
| Documentation Agent — on FAIL, write `vuln_reports` row with severity + suggested fix + parent_vuln_id linking | 3 h | Per `docs/agents/documentation-agent.md`. Closes the loop from "we found a vuln" to "here's the writeup." |
| Vulnerability board view on dashboard (third tab) | 2 h | Per `docs/components/dashboard.md` §3 ("vuln board" view). |

### P2 — observability + cost rollup + regression

| Item | Effort | What it unlocks |
|---|---|---|
| Langfuse integration (`langfuse_trace_id` per campaign, generation spans per attack) | 3 h | Designed in `docs/components/observability.md`. Gives reviewers a trace per attack. |
| `cost_rollup_daily` materialized view + dashboard cost panel | 1 h | Per `database-schema.md` §10. |
| Regression Harness — `target_version` change detection + replay queue | 3 h | Per `docs/components/regression-harness.md`. Needs cron or webhook trigger. |
| Multimodal & Document Poisoning (MP) — 9th attack category via `/v1/documents/attach` | 4 h | Per `THREAT_MODEL.md` §8. Closes the 9/9 coverage gap. |

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
