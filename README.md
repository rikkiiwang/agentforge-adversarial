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
| Per-block design docs | Skeletons under `docs/`; content rolling in |
| MVP implementation | Not started |

---

## Quick links

- **Co-Pilot target (deployed):** https://copilot-production-b532.up.railway.app/
- **OpenEMR fork (deployed):** https://openemr-production-0c8c.up.railway.app/
- **Week 3 spec:** `~/Desktop/Gauntlet/Week3/Week 3 - AgentForge - Adversarial AI Security Platform.pdf`
