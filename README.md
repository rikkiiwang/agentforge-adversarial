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
| `agentforge_adversarial/` | MVP source — runner, queue, target client, Judge ensemble, Red Team mutator. |
| `dashboard/` | Streamlit dashboard reading from Postgres `attack_runs`. |
| `evals/cases/` | 8 seed YAML test cases (one per shipped category). |
| `migrations/` | Postgres schema (subset of the canonical design in `docs/components/database-schema.md`). |
| `tests/` | 28 passing tests (unit + live-Postgres integration). |
| `docs/agents/` | Per-agent detailed design (Orchestrator, Red Team swarm, Judge, Documentation). |
| `docs/components/` | Detailed design for non-agent components (synthesis pipeline, queues, regression harness, schema, dashboard, observability). |
| `docs/taxonomy/` | One file per attack category — quality bars, channels, defenses tested, seed attack examples. |

---

## Relationship to OpenEMR Clinical Co-Pilot

| | This repo | Co-Pilot repo |
|---|---|---|
| Role | Attacker | Target |
| Language (designed) | Python (LangGraph + FastAPI control plane) | PHP (OpenEMR) + Python (`copilot/`) |
| Language (MVP) | Python (plain async `runner.py` + Streamlit dashboard) — LangGraph deferred, see [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | same |
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

Vertical slice of the platform: 8 of 9 designed attack categories,
5-seed-per-category × 3-LLM-mutations = 32 attacks per `--mutate` campaign,
dispatched against the deployed Clinical Co-Pilot, verdicts written by an
**ensemble Judge (keyword + gpt-4o-mini LLM, every category)** to
Postgres, surfaced on a Streamlit dashboard.

### What's in the MVP

| Component | MVP coverage |
|---|---|
| Postgres `campaigns` / `attack_queue` / `attack_runs` | ✅ subset of `docs/components/database-schema.md` (3 of 9 designed tables); atomic CHECK + DEFERRABLE FK enforced |
| Dispatcher INSERTs / Judge UPDATEs with atomic CHECK | ✅ two-phase write enforced by `attack_runs_judge_atomic` |
| Attack-category coverage | ✅ **8 of 9** — Prompt Injection · Data Exfiltration · Tool Misuse · State Corruption · Identity & Role · DoS & Cost · Observability Leak ⭐ · Verification-Gate Bypass ⭐. Deferred: Multimodal & Document Poisoning (needs `/v1/documents/attach` channel, not `/v1/chat`) |
| Keyword Judge | ✅ category-specific marker sets + DoS length predicate (response > 2000 chars) |
| LLM Judge (gpt-4o-mini) | ✅ runs on **every** category; SYSTEM_PROMPT is category-general and uses each seed's `expected_failure_mode` as the per-category rubric anchor |
| Ensemble Judge | ✅ keyword + LLM combined; agreement / disagreement recorded in `judge_reasoning` |
| Red Team mutator subagent (gpt-4o-mini) | ✅ via `--mutate` — 3 variants per seed, `source='random'` |
| Streamlit dashboard | ✅ KPIs + heat-map + filterable run table + drill-down |
| Live Co-Pilot target | ✅ via `--live` + `COPILOT_SESSION_ID` (reuses an iframe-obtained session) |
| Mock target (`MockCopilotClient`) | ✅ default fallback; deliberate vulnerabilities for end-to-end testing without burning live sessions |

**Headline deferred items:** LangGraph orchestration, in-dashboard Launch
button (operator console), Documentation Agent, class-probe fan-out,
regression harness, Langfuse traces. Detailed matrix and effort estimates
in [`IMPLEMENTATION.md`](IMPLEMENTATION.md).

### Setup

```bash
cp .env.example .env                    # fill in OPENAI_API_KEY (optional)
make venv                               # create .venv with Python 3.11
make install                            # pip install -e ".[dev]"
make up                                 # docker compose up -d (Postgres :5433)
make init-db                            # apply migrations/001_initial.sql
```

### Run a campaign — mock target (default)

```bash
make run                                # 5 seeds against MockCopilotClient
make run-mutate                         # 5 seeds + 15 LLM-mutated variants
make dashboard                          # http://localhost:8501
```

### Run a campaign — deployed Co-Pilot target

The deployed Co-Pilot is gated by SMART OAuth + physician-panel checks. We
reuse a session obtained from the OpenEMR iframe so the adversarial harness
inherits valid auth without owning the OAuth dance.

1. Open OpenEMR → patient chart → launch the Clinical Co-Pilot iframe.
2. Browser devtools → Network → find the `POST /v1/sessions` response.
3. Copy the `session_id` UUID from the response body.
4. Export and run:

   ```bash
   export COPILOT_SESSION_ID=<uuid-from-step-3>
   make run-live
   ```

The same campaign runs end-to-end against the deployed Co-Pilot. Sessions
are short-lived; if attacks start returning `[DISPATCH_ERROR]`, refresh
the session and re-run.

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
the Streamlit service; the CLI still runs from your laptop (it needs the
short-lived `COPILOT_SESSION_ID` from the browser).

1. **Create the Railway project**
   - railway.app → New Project → name it `agentforge-adversarial`.
   - **+ New → Database → PostgreSQL** inside the project.
   - Copy `Postgres → Variables → DATABASE_URL` (the public/connect URL,
     not the internal one).

2. **Migrate schema + populate from your laptop**

   ```bash
   export RAILWAY_DATABASE_URL='postgresql://...railway.app:.../railway?sslmode=require'

   # Apply migrations to Railway Postgres
   DATABASE_URL="$RAILWAY_DATABASE_URL" .venv/bin/python -m agentforge_adversarial init-db

   # Refresh COPILOT_SESSION_ID from the iframe devtools, then:
   export COPILOT_SESSION_ID=<fresh-uuid>
   DATABASE_URL="$RAILWAY_DATABASE_URL" .venv/bin/python \
     -m agentforge_adversarial run --cases evals/cases --mutate --live
   ```

3. **Deploy the Streamlit service**
   - Inside the same Railway project: **+ New → GitHub Repo →
     `rikkiiwang/agentforge-adversarial`**.
   - Variables on the new service:
     - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (template ref auto-injects).
     - `TARGET_URL` = `https://copilot-production-b532.up.railway.app`.
   - Railway picks up `railway.toml` (Nixpacks build, Streamlit start
     command, `/healthz` healthcheck).
   - Settings → Networking → **Generate Domain** for the public URL.

4. **Verify**
   - Open the public URL → KPIs + heat-map + drill-down all populated.
   - Dashboard caption shows `DB: Railway` and the target URL.

### What's deferred from the full design

See **[`IMPLEMENTATION.md`](IMPLEMENTATION.md)** for the full status matrix
(per-component MVP coverage, live verification results, prioritized gap to
final submission with effort estimates).

Short list of deferred items: LangGraph orchestration · in-dashboard
Launch (operator console) · Orchestrator scoring · synthesize_fn pipeline ·
class-probe · regression harness · Langfuse traces · pgvector novelty ·
Documentation Agent. Refer to `ARCHITECTURE.md` for the full design intent.

---

## Quick links

- **Co-Pilot target (deployed):** https://copilot-production-b532.up.railway.app/
- **OpenEMR fork (deployed):** https://openemr-production-0c8c.up.railway.app/
- **Week 3 spec:** `~/Desktop/Gauntlet/Week3/Week 3 - AgentForge - Adversarial AI Security Platform.pdf`
