# AgentForge — Adversarial AI Security Platform

A multi-agent adversarial evaluation platform that continuously discovers,
evaluates, and documents vulnerabilities in the OpenEMR Clinical Co-Pilot
([github.com/rikkiiwang/openemr](https://github.com/rikkiiwang/openemr)) — the
Week 1 / Week 2 deliverable in the Gauntlet AI Austin Admission Track.

This repo holds the **attacker**. The Co-Pilot under test is a separate
deployment. The two systems are connected only by HTTP — the platform speaks
to the live Co-Pilot exactly as a malicious user or compromised data source
would.

---

## What lives here

| Path | Purpose |
|---|---|
| `ARCHITECTURE.md` | Multi-agent platform design — 4 agents, LangGraph state machine, Postgres persistence, dashboards. **Hard-gate deliverable.** |
| `THREAT_MODEL.md` | Structured attack-surface taxonomy — 9 categories, OWASP / MITRE ATLAS cross-references. **Hard-gate deliverable.** |
| `USERS.md` | Target user of *this* platform (security engineer / red-team operator) and the workflows it supports. |
| `IMPLEMENTATION.md` | Implementation status as of submission — per-component MVP coverage matrix, live verification results, prioritized gap to final submission. |
| `agentforge_adversarial/` | Platform source — LangGraph 7-node state machine (`graph.py`), queue, multi-target factory (`target.py` / `targets.py`), Judge ensemble (`judges/`), Red Team swarm (`red_team/mutator.py` + `class_probe.py`, history-aware), Documentation Agent (`documentation_agent.py`), regression harness (`regression.py`), cost tracker (`cost.py`). |
| `dashboard/` | Streamlit dashboard reading from Postgres `attack_runs`. |
| `evals/cases/` | **32 seed YAML test cases:** 8 hand-curated (one per shipped category) + 15 Garak-derived (`evals/cases/garak/`) + 5 JailbreakBench (`evals/cases/jailbreakbench/`) + 4 HouYi indirect-injection (`evals/cases/houyi/`). |
| `migrations/` | Postgres schema, 6 SQL files (001 core → 002 targets → 003 lineage → 004 vuln lifecycle → 005 cost columns → 006 mock target). Subset of `docs/components/database-schema.md`. |
| `tests/` | **71 passing tests** (unit + live-Postgres integration). Run via `.venv/bin/pytest -q`. |
| `docs/agents/` | Per-agent detailed design (Orchestrator, Red Team swarm, Judge, Documentation). |
| `docs/components/` | Detailed design for non-agent components (synthesis pipeline, queues, regression harness, schema, dashboard, observability). |
| `docs/taxonomy/` | One file per attack category — quality bars, channels, defenses tested, seed attack examples. |

---

## Relationship to OpenEMR Clinical Co-Pilot

| | This repo | Co-Pilot repo |
|---|---|---|
| Role | Attacker | Target |
| Language | Python (LangGraph state machine in `agentforge_adversarial/graph.py` + Streamlit dashboard) | PHP (OpenEMR) + Python (`copilot/`) |
| Communication | HTTPS `POST /v1/chat` reusing a session_id from the Co-Pilot iframe | n/a |
| Data shared | Test results, vuln reports, regression schedule | Live patient context (Synthea synthetic data only) |

The Week 3 PRD calls for a fork of OpenEMR. We are interpreting that loosely:
the Co-Pilot fork remains at `rikkiiwang/openemr`; this adversarial platform
is a separate repo. The README of each links to the other.

---

## Status

| Stage | State |
|---|---|
| Architecture design | `ARCHITECTURE.md` |
| Threat model | `THREAT_MODEL.md` (9 categories) |
| Per-block design docs | `docs/agents/` + `docs/components/` |
| MVP implementation | **Shipped 2026-05-12** — full status matrix + gap-to-final in [`IMPLEMENTATION.md`](IMPLEMENTATION.md) |
| Deployed dashboard | https://agentforge-adversarial-production.up.railway.app/ |

---

## MVP run (2026-05-12)

> **Scope honesty:** this is an **architecture-aligned vertical slice**
> of `ARCHITECTURE.md`, not the full multi-agent platform. The
> load-bearing pieces are shipped — LangGraph state machine, Postgres
> queue/run two-phase write, ensemble Judge, Documentation Agent, vuln
> lifecycle, multi-target factory, cost rollup, FAIL/PARTIAL fan-out —
> but the **full Orchestrator scoring engine, synthesize_fn pipeline,
> `regression_schedule` cron, and Langfuse tracing remain as final
> work** beyond this submission. See `IMPLEMENTATION.md` for the
> per-component matrix; section §"P2 deferred" lists what's not in.

Vertical slice of the platform: 8 of 9 designed attack categories,
**32 seeds × ~3-LLM-mutations** per default campaign, dispatched
against any registered target (mock, deployed Co-Pilot, generic_chat,
openai_compat), verdicts written by an **ensemble Judge (keyword +
gpt-4o-mini LLM, every category)** to Postgres, surfaced on a
Streamlit dashboard.

### What's in the MVP

| Component | MVP coverage |
|---|---|
| Postgres schema | ✅ **6 of ~10 designed tables**: `campaigns` (with `total_cost_usd / total_tokens_in / total_tokens_out` cost columns), `attack_queue`, `attack_runs` (with `parent_id` / `round_num` lineage), `targets`, `vulnerabilities`, `vuln_reports`. Atomic CHECK + DEFERRABLE FK enforced. **Missing for final:** `near_misses`, `cross_regressions`, `threat_model_cells`, `cost_rollup_daily` view, `regression_schedule` (per `docs/components/database-schema.md`). |
| Dispatcher INSERTs / Judge UPDATEs with atomic CHECK | ✅ two-phase write enforced by `attack_runs_judge_atomic` |
| Attack-category coverage | ✅ **8 of 9** — Prompt Injection · Data Exfiltration · Tool Misuse · State Corruption · Identity & Role · DoS & Cost · Observability Leak ⭐ · Verification-Gate Bypass ⭐. Deferred: Multimodal & Document Poisoning (needs `/v1/documents/attach` channel, not `/v1/chat`) |
| Keyword Judge | ✅ category-specific marker sets + DoS length predicate (response > 2000 chars) |
| LLM Judge (gpt-4o-mini) | ✅ runs on **every** category; SYSTEM_PROMPT is category-general and uses each seed's `expected_failure_mode` as the per-category rubric anchor |
| Ensemble Judge | ✅ keyword + LLM combined; agreement / disagreement recorded in `judge_reasoning` |
| Red Team mutator subagent (gpt-4o-mini) | ✅ 3 mutations per seed, `source='random'`, parallel via `asyncio.gather` |
| Red Team class-probe subagent (gpt-4o-mini) | ✅ 10 boundary variants per FAIL, `source='class_probe'`, parent lineage via `attack_runs.parent_id`, parallel via `asyncio.gather` |
| Red Team partial-reentry subagent (gpt-4o-mini) | ✅ 3 fresh phrasings per PARTIAL to disambiguate ambiguous verdicts, parent lineage via `attack_runs.parent_id` |
| LangGraph state machine | ✅ 7 nodes (load_seeds → mutate → dispatch → judge → partial_reentry / class_probe / bump_round → dispatch). Two conditional edges: `decide_after_judge` (PARTIAL/FAIL/END) + `decide_after_partial_reentry` (class_probe/bump_round). Bounded by `--max-rounds`. |
| Multi-target via `targets` table | ✅ 3 target types: `copilot` (auto-creates `/v1/sessions`), `generic_chat` (any HTTP/JSON LLM with a prompt template), `openai_compat` (OpenAI Chat Completions wire format) |
| Streamlit dashboard | ✅ 4-tab layout (📊 Coverage / 🛡️ Vulns / 🎯 Runs / 🚀 Launch) with persistent sidebar carrying campaign scope, live KPIs (Total / FAIL / PARTIAL / PASS), red-team LLM cost tile, and active-campaign progress fragment |
| Documentation Agent | ✅ writes `vulnerabilities` + `vuln_reports` rows on every FAIL; severity weighted by clinical-safety impact; lineage via `attack_runs.parent_id` |
| Seed corpus | ✅ **32 seeds** (8 hand-curated + 15 Garak + 5 JailbreakBench + 4 HouYi) across 8 categories |
| Cost rollup | ✅ ContextVar threads `campaign_id` through `asyncio.gather`; per-1M-token price table (2026-05 snapshot) for gpt-4o / gpt-4o-mini / gpt-4.1-mini; sidebar cost tile + token caption |
| Regression harness | ✅ `python -m agentforge_adversarial regress` CLI + Vuln-Board "🔁 Replay" button; PASS → fix_validated, FAIL → reopened, PARTIAL → no change |
| History-aware Red Team prompts | ✅ rounds 1+ feed REFUSED/SUCCEEDED same-category context into mutator + class-probe LLM prompts to bias away from already-defended framings |

**Headline deferred items:** Langfuse traces (P2 — needs external account),
Multimodal & Document Poisoning category (P2 — needs target-side
`/v1/documents/attach`), pgvector novelty dedup (not planned),
`regression_schedule` table + cron (slim regression-harness shipped instead).
P0 + P1 shipped 2026-05-13; P2 shipped 2026-05-14. Detailed matrix in
[`IMPLEMENTATION.md`](IMPLEMENTATION.md).

### Setup

```bash
cp .env.example .env                    # fill in OPENAI_API_KEY (optional)
make venv                               # create .venv with Python 3.11
make install                            # pip install -e ".[dev]"
make up                                 # docker compose up -d (Postgres :5433)
make init-db                            # apply all migrations/*.sql in order (idempotent)
```

### Run a campaign — mock target (frictionless first-run)

The seeded `Mock Co-Pilot (local stub)` target (`migrations/006_mock_target.sql`)
runs `MockCopilotClient` in-process — no env vars, no network. It seeds
deliberate vulnerabilities across the MVP categories so the Judge has
something to score.

```bash
make run                                # 32 seeds + 3 mutations each → mock target
make run-no-mutate                      # 32 seeds only (mutator off) → mock target
make dashboard                          # http://localhost:8501
```

For an explicit target choice from the CLI:

```bash
.venv/bin/python -m agentforge_adversarial run \
  --cases evals/cases --target "Mock Co-Pilot (local stub)"
```

### Run a campaign — deployed Co-Pilot target

`POST /v1/sessions` on the Co-Pilot accepts a `(physician_user_id,
patient_id)` pair and returns a session_id. The physician-panel gate lets
`physician_user_id="admin"` through unconditionally. The harness creates
its own sessions; you only have to tell it which Synthea patient to
anchor them to.

1. Open OpenEMR → patient list (any patient) → copy the patient UUID
   from the URL (the `pid` query param, e.g. `0fe1a5d2-...`).
2. Configure once:

   ```bash
   export COPILOT_PATIENT_ID=<uuid-from-step-1>
   # Optional override; default is "admin":
   # export COPILOT_PHYSICIAN_USER_ID=admin
   ```

3. Click **▶ Run campaign** in the dashboard's 🚀 Launch tab, or run
   `make run-live` from the CLI. `make_client()` will pick up
   `$COPILOT_PATIENT_ID` as a fallback when the target row's
   `config_json.patient_id` is empty (which is how the seeded row ships).

To make the patient_id durable instead of relying on the env var:

```bash
.venv/bin/python -m agentforge_adversarial update-target \
  --name "OpenEMR Clinical Co-Pilot (deployed)" \
  --patient-id <uuid-from-step-1>
```

Sessions are recreated automatically if they expire mid-campaign — the
client retries once on a 404 from `/v1/chat` and continues.

### Inspect results

```bash
make dashboard                          # Streamlit on http://localhost:8501
```

Or via psql:

```bash
docker compose exec postgres psql -U agentforge -d agentforge -c \
  "SELECT case_id, category, source, judge_verdict, judge_rubric_version FROM attack_runs ORDER BY created_at DESC;"
```

### Deploy the dashboard to Railway

The dashboard is meant to be shareable so reviewers don't have to clone
+ `make up` to see results. Topology: Railway hosts managed Postgres +
the Streamlit service; the Launch panel inside the dashboard spawns
campaigns as subprocesses of the Streamlit container itself — no
laptop-side CLI is needed once `COPILOT_PATIENT_ID` is configured.

1. **Create the Railway project**
   - railway.app → New Project → name it `agentforge-adversarial`.
   - **+ New → Database → PostgreSQL** inside the project.
   - Copy `Postgres → Variables → DATABASE_URL` (the public/connect URL,
     not the internal one).

2. **Apply schema** (re-run after every new migration; the command is
   idempotent so this is safe to run on every deploy):

   ```bash
   export RAILWAY_DATABASE_URL='postgresql://...railway.app:.../railway?sslmode=require'
   DATABASE_URL="$RAILWAY_DATABASE_URL" .venv/bin/python -m agentforge_adversarial init-db
   ```

   `init-db` runs every `migrations/*.sql` in order; the current set is
   001 (core schema) → 002 (targets) → 003 (lineage) → 004 (vuln
   lifecycle) → 005 (cost rollup) → 006 (mock target seed). Each
   migration is wrapped in `DO $$ BEGIN ... EXCEPTION WHEN ...` blocks
   or `IF NOT EXISTS` guards, so running this against a populated DB
   is a no-op for already-applied rows.

3. **Deploy the Streamlit service**
   - Inside the same Railway project: **+ New → GitHub Repo →
     `rikkiiwang/agentforge-adversarial`**.
   - Variables on the new service:
     - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (template ref auto-injects).
     - `TARGET_URL` = `https://copilot-production-b532.up.railway.app`.
     - `COPILOT_PATIENT_ID` = a Synthea patient UUID from the OpenEMR
       patient-list URL (the `pid` query param).
     - `OPENAI_API_KEY` = your key (the in-container subprocess uses it
       for the Red Team mutator and LLM Judge).
   - Railway picks up `railway.toml` (Nixpacks build, Streamlit start
     command, `/healthz` healthcheck).
   - Settings → Networking → **Generate Domain** for the public URL.

4. **Verify**
   - Open the public URL → Launch panel renders at the top.
   - Click **▶ Run campaign** (Live) → progress bar fills → heat-map +
     KPIs populate.
   - Dashboard caption shows `DB: Railway` and the target URL.

### What's deferred from the full design

See **[`IMPLEMENTATION.md`](IMPLEMENTATION.md)** for the full status matrix
(per-component MVP coverage, live verification results, prioritized gap to
final submission with effort estimates).

Short list of deferred items: Orchestrator scoring · synthesize_fn pipeline ·
Langfuse traces (needs external account) · pgvector novelty · Multimodal /
document-poisoning channel (needs target-side endpoint) ·
`regression_schedule` table + cron (slim CLI + button shipped instead).
Refer to `ARCHITECTURE.md` for the full design intent.

---

## Quick links

- **Co-Pilot target (deployed):** https://copilot-production-b532.up.railway.app/
- **OpenEMR fork (deployed):** https://openemr-production-0c8c.up.railway.app/
- **Week 3 spec:** `~/Desktop/Gauntlet/Week3/Week 3 - AgentForge - Adversarial AI Security Platform.pdf`
