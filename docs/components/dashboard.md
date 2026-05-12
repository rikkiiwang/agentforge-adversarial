# Dashboard

**Status:** Design-of-record, draft 2026-05-12. Referenced from
`ARCHITECTURE.md` §7, §9, §13. Three views + cost tile + approval
gates, all over the Postgres source of truth.

---

## 1. Purpose

Surface the platform's state to the **AI security engineer** (per
`USERS.md` primary user) and provide the human-in-the-loop control
points the architecture requires. The dashboard is:

- **The only UI surface humans use** to operate the platform (campaign
  approvals, vuln triage, fix sign-off, regression overrides).
- **A direct read on the Postgres source of truth**, not a separate
  cache. Three views = three SQL aggregations.
- **The substrate the Orchestrator reads** (per ARCHITECTURE.md §7) —
  same Postgres state powers the heat-map for both human eyes and
  agent planning.

---

## 2. Three views

### 2.1 Coverage heat-map

The **denominator-aware** view of testing breadth.

- **Rows:** the 9 categories from `threat_model_cells.category`.
- **Columns:** the subcategories (or the 5 default channels rolled up,
  depending on zoom level).
- **Cells:** each `(category, subcategory)` cell colored by state:
  - `untested` (gray) — 0 attempts on current `target_version`
  - `under_tested` (light) — fewer attempts than quality-bar floor
  - `tested-held` (green) — full quality bars, all PASS
  - `wavering` (amber) — partial-rate > 25% over last 30 days
  - `exploited` (red) — open vuln on current `target_version`
  - `regression` (dark red) — `reopened` vuln on current version

Each cell carries:
- Quality-bar fraction (`3/5` channels touched)
- Diversity score `d=0.42` (mean pairwise embedding distance among
  attempted attacks in the cell)
- Last-tested timestamp

**Drill-down level 1:** click a cell → channel breakdown (per
`(category, subcategory, channel)` triple), with per-channel pass /
partial / fail counts, state, last run timestamp, and the
Orchestrator's recommended next move (sourced from
`docs/agents/orchestrator.md` §5 scoring).

**Drill-down level 2:** click a channel row → individual `attack_runs`
table: `case_id`, summary, `parent_id` lineage, source, verdict, link
to open vuln (if any), one-click jump to the Langfuse trace.

The heat-map is rendered by a single SQL aggregation:

```sql
SELECT category, subcategory, target_version,
       count(*) FILTER (WHERE judge_verdict = 'pass')    AS pass_count,
       count(*) FILTER (WHERE judge_verdict = 'partial') AS partial_count,
       count(*) FILTER (WHERE judge_verdict = 'fail')    AS fail_count,
       count(DISTINCT channel)                            AS channels_touched,
       <diversity_score subquery>                         AS diversity_score
  FROM attack_runs
 WHERE category_validated = true
   AND target_version = $current_target_version
 GROUP BY category, subcategory, target_version;
```

State color is derived in the rendering layer from the columns above
+ a join on `vulnerabilities` for open-vuln detection.

### 2.2 Near-Miss tile

The **active near-misses being explored**.

- Rows from `near_misses WHERE state = 'exploring'`.
- Shows: vuln class trace, parent PARTIAL attack, variant count, round
  number, cumulative cost, state machine progress.
- Click a row → drill into the lineage tree (PARTIAL → variants →
  judged outcomes per variant).
- When a near-miss escalates to a vuln, the row transitions to
  `state='escalated'` and moves to the Vuln Board (with a link back).

### 2.3 Vuln Board

The **state machine view** for confirmed exploits.

- Rows from `vulnerabilities JOIN vuln_reports`.
- Columns: vuln id, category, severity, state, target_version,
  assignee, age, linked class-probe variant count.
- Group-by toggle: by category / by severity / by state / by parent
  (groups class-probe variants under their root).
- Click a row → vuln detail page: full report (severity rationale,
  observed vs expected, repro, suggested fix, defense reference),
  state transition log, regression history.

State-transition actions are gated per ARCHITECTURE.md §13:
- `discovered → triaged` button is **enabled for operators**
- `triaged → fix_proposed` button is **enabled for operators with a
  commit/PR reference field**
- `fix_proposed → fix_validated` is **never operator-clickable** (only
  the Regression Harness writes this)
- `→ closed` requires explicit confirmation modal with severity-aware
  language ("This vuln is CRITICAL. Confirm clinical sign-off has been
  obtained.")

---

## 3. Cost tile

A small permanent strip across the top of the dashboard:

- **$ spent today** / **$ remaining in daily caps** (per category +
  total).
- **Cost-per-finding** (rolling 7-day average): how much money it
  takes to discover one vuln in each category. The platform's CISO-
  defensible efficiency metric.
- **Per-agent burn rate** breakdown: Red Team Swarm $X / Judge $Y /
  Documentation Agent $Z.
- Alerts: a category whose daily pool is >80% consumed gets an amber
  highlight; >100% consumed greys out the category until tomorrow.

Backed by a materialized view (`cost_rollup_daily`) refreshed every
60 seconds.

---

## 4. Approval gates — where humans say yes/no

The platform's trust-boundary contracts (per ARCHITECTURE.md §9) all
flow through the dashboard. Three explicit gate UIs:

### 4.1 Campaign approval (when `swarm_approval_mode != 'auto'`)

When the Orchestrator emits a `CampaignBrief` with a `SwarmRecommendation`
that diverges from the operator's default (mode `review_recommended`)
or always (mode `review_all`), the dashboard displays:

- The `CampaignBrief`: cell, budget, seed strategy, scoring breakdown
- The `SwarmRecommendation`: proposed composition + rationale per
  subagent + base config source
- **Three buttons:** Accept · Modify · Override
  - **Accept** → dispatch as recommended
  - **Modify** → open inline swarm-spec editor; dispatch with edited
    spec; the edit does NOT update `config/swarm.yaml` (one-time
    override only)
  - **Override** → write a fully custom swarm-spec for this campaign

A `dispatch_timeout` (default 5 min) auto-accepts if operator does
nothing. Configurable per category.

### 4.2 High-severity vuln report approval

When the Documentation Agent writes a vuln with severity = HIGH or
CRITICAL, the row appears in the Vuln Board with a yellow "pending
review" badge. The operator must:

1. Click into the report
2. Review observed vs expected, repro steps, suggested fix
3. Click **Approve & file** (moves to `triaged`) OR **Reject as false
   positive** (sets `human_approved = false`, feeds back to Judge
   calibration)

Until approved, the vuln does NOT appear in any external integration
(no Jira sync, no Slack alert, no CSV export). This is the gate that
keeps the platform from inflating its own importance.

### 4.3 Reopen acknowledgement

When the Regression Harness writes `vulnerabilities.state = 'reopened'`
(per `docs/components/regression-harness.md` §6), the vuln re-enters
the active rotation but **requires operator acknowledgement** before
the Orchestrator schedules new campaigns for that class. This prevents
a single regression event from cascading into a multi-day Red-Team
loop while the engineering team is still investigating.

The acknowledgement UI shows:
- The original closing context (who closed it, when, what fix
  validated it)
- The regression event (which target_version, which replay verdict,
  the cross-regression report if applicable)
- **Two buttons:** Acknowledge & resume probing · Disable schedule
  pending investigation

---

## 5. Implementation

The dashboard is a separate web service, not part of the LangGraph
runtime. It only **reads** Postgres + writes a narrow set of state
transitions through an authorization-checked API.

```
┌────────────────────────────┐
│  Browser (operator)         │
└─────────────┬──────────────┘
              │ HTTPS
              ▼
┌────────────────────────────┐
│  Dashboard FastAPI service │
│  - GET /heatmap            │
│  - GET /vulns/:id          │
│  - POST /vulns/:id/triage  │
│  - POST /campaigns/:id/    │
│         approve            │
│  - POST /vulns/:id/        │
│         reopen-ack         │
└─────────────┬──────────────┘
              │ SQL
              ▼
┌────────────────────────────┐
│   Postgres                  │
└────────────────────────────┘
```

Frontend: a simple React/HTMX UI (no real-time WebSockets for MVP —
polling every 30s suffices for the use cases). Auth: a single
operator-token model; for production, OAuth integration with the
operator's SSO.

The dashboard is **NOT** invoked from inside any LangGraph node. The
agents write Postgres state; the dashboard reads it. Decoupled by
construction.

---

## 6. Drill-down navigation patterns

The dashboard must let an operator answer four common questions in
≤2 clicks from the home view:

| Question | Click path |
|---|---|
| "What vulns are pending my review?" | Vuln Board → filter `state = discovered` |
| "Did the latest deploy break anything?" | Heat-map → filter to `target_version = current_SHA` → look for new red cells |
| "How many partials have we accumulated in prompt_injection this week?" | Heat-map → `prompt_injection` row → Near-Miss tile filtered to that category |
| "What's our cost-per-finding trend?" | Cost tile → "Trend" sub-view (rolling 30d) |

---

## 7. Operational defaults

| Concern | Default |
|---|---|
| Heat-map refresh interval | 30 seconds polling |
| Vuln Board refresh interval | 30 seconds polling |
| Cost rollup materialization | Every 60 seconds via Postgres view |
| Approval timeout (`swarm_approval_mode='review_recommended'`) | 5 minutes |
| Auth | Operator token, validated on every API call |
| Audit logging | Every state transition emits an audit row (who clicked, when, transition); retained indefinitely |
| Failure mode | If the dashboard is down, the platform continues operating. Auto-approval modes (`auto`) keep dispatching; manual-approval modes (`review_*`) queue waiting campaigns until the dashboard comes back |
| Mobile support | Read-only mobile view for the Vuln Board (operator may need to check on an alert); state transitions desktop-only |

---

## 8. Versioning

`app/dashboard/__init__.py`:

```python
DASHBOARD_VERSION = "0.4.0"
```

Bumped on schema-breaking UI changes (e.g., new state in the vuln
state machine, new approval gate). Stored in user-facing audit logs.

---

## 9. Implementation pointers

- Backend: `app/dashboard/api.py` (FastAPI routes)
- DB queries: `app/dashboard/queries.py` (single source of truth for
  the SQL aggregations powering each view)
- Authorization: `app/dashboard/auth.py` (operator token validation)
- Frontend: `app/dashboard/static/` (Vue or HTMX templates; choice
  deferred to implementation plan)
- Tests:
  - Each view's SQL produces the correct rollup for known fixture
    data
  - State-transition POST endpoints reject unauthorized writers (e.g.,
    operator cannot write `state='fix_validated'`)
  - Approval timeout fires correctly on `review_recommended` mode
  - Audit log captures every state transition with actor + timestamp
  - Dashboard-down failure mode: agents continue operating
