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
| `docs/agents/` | Per-agent detailed design (Orchestrator, Red Team swarm, Judge, Documentation). |
| `docs/components/` | Detailed design for non-agent components (synthesis pipeline, queues, regression harness, schema, dashboard, observability). |
| `docs/taxonomy/` | One file per attack category — quality bars, channels, defenses tested, seed attack examples. |

Source code, evals, and infrastructure will land here as the implementation
proceeds (`src/`, `evals/`, `infra/`).

---

## Relationship to OpenEMR Clinical Co-Pilot

| | This repo | Co-Pilot repo |
|---|---|---|
| Role | Attacker | Target |
| Language | Python (FastAPI + LangGraph) | PHP (OpenEMR) + Python (`copilot/`) |
| Communication | HTTPS to Co-Pilot's chat + FHIR endpoints | n/a |
| Data shared | Test results, vuln reports, regression schedule | Live patient context (Synthea synthetic data only) |

The Week 3 PRD calls for a fork of OpenEMR. We are interpreting that loosely:
the Co-Pilot fork remains at `rikkiiwang/openemr`; this adversarial platform
is a separate repo. The README of each links to the other.

---

## Status

| Stage | State |
|---|---|
| Architecture defense | Draft in `ARCHITECTURE.md` |
| Threat model | Draft in `THREAT_MODEL.md` |
| Per-block design docs | In `docs/agents/` + `docs/components/` |
| MVP implementation | Shipped (2026-05-12) — see below |

---

## MVP run (2026-05-12)

Thin vertical slice of the platform: seed attacks across three categories,
hit a target, ensemble Judge writes verdicts to Postgres, Streamlit
dashboard reads from Postgres for the demo view.

### What's in the MVP

| Component | MVP coverage |
|---|---|
| Postgres `campaigns` / `attack_queue` / `attack_runs` | ✅ (subset of `docs/components/database-schema.md`) |
| Dispatcher INSERTs / Judge UPDATEs with atomic CHECK | ✅ |
| Keyword Judge for 3 categories | ✅ |
| LLM Judge (gpt-4o-mini, prompt_injection only) + ensemble | ✅ (active when `OPENAI_API_KEY` is set) |
| Red Team mutator subagent (gpt-4o-mini) | ✅ (`--mutate` flag) |
| Streamlit dashboard | ✅ |
| Live Co-Pilot target | ✅ via `--live` + `COPILOT_SESSION_ID`. Reuses a session obtained from the OpenEMR iframe (see "Run against the live target" below). |
| Mock target (`MockCopilotClient`) | ✅ default fallback. Mimics the API shape with deliberately seeded vulnerabilities so the platform is demoable without burning live sessions. |

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

LangGraph orchestration · Orchestrator scoring · synthesize_fn pipeline ·
class-probe · regression harness · Langfuse · pgvector novelty · Ollama
swarm · Documentation Agent. See `ARCHITECTURE.md` for the full picture.

---

## Quick links

- **Co-Pilot target (deployed):** https://copilot-production-b532.up.railway.app/
- **OpenEMR fork (deployed):** https://openemr-production-0c8c.up.railway.app/
- **Week 3 spec:** `~/Desktop/Gauntlet/Week3/Week 3 - AgentForge - Adversarial AI Security Platform.pdf`
