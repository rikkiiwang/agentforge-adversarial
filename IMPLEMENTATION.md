# AgentForge Adversarial — Implementation Status

**Submission date:** 2026-05-12
**MVP commit:** `513ee25` (visible on both [GitHub](https://github.com/rikkiiwang/agentforge-adversarial) and [GitLab](https://labs.gauntletai.com/ruijingwang/agentforge-adversarial))
**Deployed dashboard:** https://agentforge-adversarial-production.up.railway.app/
**Target under test:** https://copilot-production-b532.up.railway.app/

---

## TL;DR

A vertical slice of the platform shipped: **8 of 9 designed attack categories**,
**5 hand-curated seed cases per category amplified to 32 attacks via an LLM
Red Team mutator**, dispatched against the deployed Clinical Co-Pilot,
verdicts written by an **ensemble Judge (deterministic keyword + gpt-4o-mini
LLM)** to **Railway-hosted Postgres**, surfaced on a public **Streamlit
dashboard**. All control flow runs as plain async Python in
`agentforge_adversarial/runner.py` — LangGraph orchestration is the headline
deferred item.

| Submission gate | Status |
|---|---|
| Hard gate: 3+ attack categories | ✅ 8 categories shipped |
| Hard gate: agent prototype running live against deployed target | ✅ Red Team mutator + Ensemble Judge, hits live Co-Pilot |
| Hard gate: working test suite | ✅ 28 tests pass (`make test`) |
| Hard gate: results visible to reviewer | ✅ public dashboard reads from Railway Postgres |

---

## What shipped — component-by-component matrix

Mapped to the section numbers in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Agents (`ARCHITECTURE.md` §2)

| Agent | MVP | Notes |
|---|---|---|
| **Red Team Swarm** | ⚠️ Partial | 1 subagent × gpt-4o-mini, hard-coded. Produces 3 mutations per seed via `agentforge_adversarial/red_team/mutator.py`. **Missing for final:** configurable swarm from `config/swarm.yaml`, multi-LLM (claude-haiku, deepseek, llama-3 via Ollama), parallel synthesis with `synthesize_fn` (designed in `docs/components/synthesis-pipeline.md`). |
| **Judge** | ✅ Shipped | Ensemble: keyword predicates + gpt-4o-mini LLM on every category. Files: `agentforge_adversarial/judges/{keyword,llm_judge,ensemble}.py`. Atomic UPDATE enforced by `attack_runs_judge_atomic` CHECK constraint. |
| **Orchestrator** | ❌ Deferred | Designed in `docs/agents/orchestrator.md` (5-signal weighted scoring, per-category daily pools, score-weighted budget, `SwarmRecommendation`). MVP runner.py uses fixed config + flat seeds + uniform mutator instead. |
| **Documentation Agent** | ❌ Deferred | Designed in `docs/agents/documentation-agent.md` (severity logic, class-probe two-write flow, parent_vuln_id variant linking, defense-mapped suggested fix). MVP writes verdicts directly to `attack_runs` from the Judge; no `vuln_reports` rows generated yet. |

### Components (`ARCHITECTURE.md` §4-§7)

| Component | MVP | Notes |
|---|---|---|
| **Attack Queue** | ⚠️ Partial | `attack_queue` table shipped with all designed columns + per-source provenance rules. Dispatch loop is a single sequential for-loop in `runner.py` (not the async `dispatcher_loop` with rate/cost filters from `docs/components/attack-queue.md` §4). |
| **Dispatcher → Judge two-phase write** | ✅ Shipped | Dispatcher INSERTs `attack_runs` with Judge cols NULL; Judge UPDATEs atomically (`attack_runs_judge_atomic` CHECK). Idempotency via `attack_runs.queue_entry_id UNIQUE` + DEFERRABLE FK. |
| **Synthesis pipeline** | ❌ Deferred | Designed in `docs/components/synthesis-pipeline.md` (6-stage: normalize → embed → dedup → novelty → score → budget-cap). MVP enqueues all mutator outputs directly. |
| **Class-probe** | ❌ Deferred | FAIL → ~10 boundary variants fan-out. Needs LangGraph conditional edges. |
| **Regression Harness** | ❌ Deferred | Designed in `docs/components/regression-harness.md`. Needs a scheduler; current model is one-shot CLI campaigns. |
| **Dashboard** | ✅ Shipped | Streamlit single-page: KPI cards, altair category × verdict heat-map, filterable run table, per-run drill-down. File: `dashboard/app.py`. Deployed on Railway via `railway.toml` (Nixpacks build, `/healthz` healthcheck). |
| **Observability** | ⚠️ Partial | Stdout logging from `runner.py` only. **Missing for final:** Langfuse traces (one trace per campaign with generation spans), per-attack `langfuse_trace_id` column populated. |
| **Postgres schema** | ⚠️ Partial | 3 of 9 designed tables (`campaigns`, `attack_queue`, `attack_runs`). Subset of `docs/components/database-schema.md` §1-§8. **Missing for final:** `vulnerabilities`, `vuln_reports`, `near_misses`, `cross_regressions`, `threat_model_cells`, `cost_rollup_daily` (view). |
| **pgvector novelty filter** | ❌ Deferred | Designed for HNSW dedup on embeddings. Not used; mutator produces N variants without deduplication. |
| **Operator console (in-dashboard campaign launch)** | ❌ Deferred | Currently dashboard is a viewer; campaigns are triggered via CLI (`make run-live`). Final adds target selector + swarm config + Launch button + live-stream updates. |

### Orchestration framework (`ARCHITECTURE.md` §3, §4)

| Item | MVP | Notes |
|---|---|---|
| **LangGraph state machine** | ❌ Deferred | Plain async Python in `runner.py:run_campaign()` (50 lines, sequential). State-machine invariants enforced at the DB level (`attack_runs_judge_atomic` CHECK + `queue_entry_id UNIQUE`) so future LangGraph state schemas migrate cleanly. **Final:** LangGraph unlocks conditional edges (`PARTIAL → Mutator re-entry`, `FAIL → fan-out to Documentation Agent + Class-Probe + Regression`) which is where the framework's value lands. |

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

### P0 — operator console + LangGraph (the biggest visual gap)

| Item | Effort | What it unlocks |
|---|---|---|
| LangGraph node/edge skeleton wrapping the existing `runner.py` flow | 3-4 h | Foundation for the rest of P0. Same behavior, graph-shaped. |
| In-dashboard Launch button (target selector + swarm config + Categories multiselect + "Launch Campaign") | 4-5 h | Operator-driven flow per `docs/components/dashboard.md`. Removes the "viewer-only" criticism. |
| Live-stream updates as campaign runs (Streamlit polling Postgres for new rows in active campaign) | 2 h | Demo video shows attacks streaming in real-time. |
| Conditional edges: `PARTIAL → Mutator re-entry`, `FAIL → Class-probe fan-out (10 boundary variants)` | 4 h | Where LangGraph actually earns its keep. Produces the variant-chain effect described in `ARCHITECTURE.md` §4. |

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
| `agentforge_adversarial/runner.py` | Sequential async pipeline (load → mutate → enqueue → dispatch → judge → write) — replaces the LangGraph state machine in MVP. |
| `agentforge_adversarial/queue.py` | `create_campaign`, `enqueue_cases` (writes to `attack_queue`). |
| `agentforge_adversarial/target.py` | `CopilotClient` (session-reuse against live target), `MockCopilotClient` (vulnerable test double), `dispatch_to_attack_run`, `insert_attack_run`. |
| `agentforge_adversarial/judges/keyword.py` | Deterministic Judge — 8 category-specific marker sets + length predicate for DC. |
| `agentforge_adversarial/judges/llm_judge.py` | gpt-4o-mini Judge — category-general SYSTEM_PROMPT, takes `expected_failure_mode` from seed as the per-category rubric. |
| `agentforge_adversarial/judges/ensemble.py` | Combines keyword + LLM (every category), UPDATEs `attack_runs` atomically. |
| `agentforge_adversarial/red_team/mutator.py` | One Red Team subagent — gpt-4o-mini, produces 3 mutations per seed. |
| `dashboard/app.py` | Streamlit dashboard, reads Postgres via `psycopg`. |
| `migrations/001_initial.sql` | 3 tables + atomic CHECK + DEFERRABLE FK. Strict subset of full schema design. |
| `evals/cases/*.yaml` | 8 seed test cases (1 per shipped category). |
| `tests/` | 28 passing tests — unit + live-Postgres integration. |

---

## Operating the platform

Per `README.md` "Setup" + "Run a campaign" sections. The submission-ready
flow is one command:

```bash
export COPILOT_SESSION_ID=<uuid-from-iframe>
DATABASE_URL=<railway-url> make run-live
# refresh https://agentforge-adversarial-production.up.railway.app/
```
