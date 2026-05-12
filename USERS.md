# Users — who this platform is for

**Status:** Draft 2026-05-11. Companion to `ARCHITECTURE.md` and
`THREAT_MODEL.md`.

---

## Primary user — the AI security engineer

A security engineer at a healthcare AI vendor or hospital IT department,
responsible for the continuous security posture of an LLM-integrated
clinical system. Typical profile:

- 3–8 years application security experience; some familiarity with LLM
  prompt-injection literature, OWASP LLM Top 10, MITRE ATLAS.
- Comfortable reading test runs, threat models, and Langfuse traces.
- **Not necessarily a clinician** — relies on the threat model + Judge
  rubrics to translate clinical-safety failures into technical signals.
- Reports to a CISO who must defend the system to a hospital board.

This person is **not the Co-Pilot's end user** (the physician). The
physician is a different user of a different product (the Co-Pilot itself).
That separation is load-bearing: this platform is built for the people
who *defend* the Co-Pilot, not the people who use it.

---

## Secondary users

| User | Role | What they do here |
|---|---|---|
| **CISO / Security director** | Read-only consumer of the dashboard | Reviews coverage trends, sign-offs on high-severity vulnerability reports, demands evidence in board presentations |
| **AI / ML engineer on the Co-Pilot team** | Recipient of vulnerability reports | Uses the platform's Documentation Agent output (repro + suggested fix) to ship patches; ships a new target version; expects the regression suite to validate the fix |
| **External auditor (HIPAA / SOC 2)** | Read-only consumer | Reads vuln reports and regression-pass history during compliance reviews; cross-references findings against OWASP / ATLAS IDs |

The platform is **not** designed for:

- The Co-Pilot's end user (the physician) — wrong product
- Cohort-level penetration testers using one-shot manual prompts — wrong
  workflow shape (the platform is for *continuous* testing, not one-time
  campaigns)
- Curious researchers running attacks on systems they don't own —
  see `docs/components/dashboard.md` for access controls on who can
  trigger campaigns

---

## Three use cases (the only three that ship)

### UC1 — Continuous coverage and regression

**Trigger:** Co-Pilot deploys a new version. The platform's Orchestrator
detects the SHA change (via the target's `/healthz`) and kicks off
campaigns prioritized by coverage gaps and open findings.

**User experience:**

1. Security engineer opens the dashboard each morning.
2. Coverage heat-map shows the latest target version; cells flag any new
   regressions or wavering trends.
3. Vuln board shows any new findings from overnight runs.
4. Engineer reviews high-severity reports queued in `discovered` state,
   approves or rejects each; rejected findings feed back to Judge
   calibration.

**Defended by:** the regression harness + weekly cron + heat-map state
machine + per-category `regression_schedule` rows. **Why automation
matters here:** a human cannot manually run 50 categories × 5 channels ×
N variants per week. The platform makes this volume tractable.

### UC2 — Targeted campaign on demand

**Trigger:** Security engineer wants to investigate a specific suspicion
(e.g., "I heard about a new persona-hijack technique on /r/llmsecurity;
can our Co-Pilot resist?") or a specific cell looks weak.

**User experience:**

1. Engineer opens the dashboard, clicks a heat-map cell, hits "Run
   campaign on this cell."
2. Configures the Red Team swarm: which subagent models, how many
   subagents, what budget. Defaults pulled from `config/synthesis.yaml`.
3. Watches Langfuse traces in real-time as subagents generate, synthesis
   dedups, target dispatches, judge verdicts roll in.
4. If a FAIL surfaces, the Documentation Agent's report appears in the
   Vuln board; engineer reviews and routes.

**Defended by:** Orchestrator's manual-campaign branch + Red Team swarm
configurability + dashboard drill-down. **Why automation matters:** the
swarm can produce N×M candidate attacks in parallel under budget; manual
prompt-crafting is rate-limited by the engineer's typing speed.

### UC3 — Defense validation after a fix

**Trigger:** Co-Pilot team ships a fix for an open vulnerability. The
vuln moves from `fix_proposed` → awaiting validation.

**User experience:**

1. Engineer opens the vuln in the Vuln board, hits "Run regression
   replay."
2. The platform replays the originating attack + all class-probe variants
   (every `attack_runs.parent_id` chain rooted at this vuln) against the
   new target version.
3. Judge emits new verdicts. If all PASS, the vuln transitions to
   `fix_validated`. If any FAIL, the vuln is `reopened` with a link
   noting which variant slipped through.
4. Cross-regression check: the platform also re-runs the regression suite
   for *other* categories to catch fix-induced regressions elsewhere.

**Defended by:** `regression_schedule` table + lineage via `parent_id` +
cross-category regression scan. **Why automation matters:** the spec is
explicit that "a test that passes because the model's behavior changed —
not because the vulnerability was actually fixed — is worse than no test
at all." Mechanical replay is the only credible way to distinguish.

---

## Out of scope for this platform

| Out of scope | Why |
|---|---|
| Attacking systems other than the Co-Pilot | Trust-and-safety boundary — the platform's `target_url` is configured once and is read-only at runtime |
| Producing attack content for offensive use outside testing | Every attack is logged + bound to a vuln-id; outputs are not exposed as a usable corpus |
| Replacing human-in-loop CISO review | The platform recommends; humans decide. No auto-applied fixes; high-severity reports gated |
| Testing the platform itself's UI usability | Not a security concern; deferred indefinitely |
| Cohort-style red-team competitions / CTF | Wrong shape — built for continuous, not episodic |
