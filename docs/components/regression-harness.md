# Regression Harness

**Status:** Design-of-record, draft 2026-05-12. Referenced from
`ARCHITECTURE.md` §4 step 7c(b), §13 (`fix_validated` / `reopened`
state writes).

---

## 1. Purpose

Convert every confirmed exploit into a **versioned, repeatable test**
that runs against every future version of the target. The harness is
how the platform answers "is the system getting more or less resilient
over time?" — a static test runner does not. Three guarantees:

- **Every FAIL is automatically replayed weekly** (configurable
  cadence per row).
- **Every new target deploy triggers a full regression sweep** (catches
  fix-induced regressions in *other* categories).
- **Fix validation is mechanical, not opinion-based** — a `vulnerabilities`
  row only moves to `fix_validated` when the original attack + all
  linked Class-probe variants replay as `PASS` on the new
  `target_version`.

This is the platform's strongest contract with the operator: once a
vuln has a `regression_schedule` row, the platform owns the *"did the
fix actually hold?"* question.

---

## 2. Inputs

| Source | When read | Purpose |
|---|---|---|
| `regression_schedule` table | Every cron tick + every new target_version event | The replay queue |
| `attack_runs` row (joined by `attack_run_id`) | Per replay | The canonical attack to replay — transcript + attack_prompt |
| `vulnerabilities` row (joined by `vuln_id`) | Per replay | Current state; parent_vuln_id walk for variants |
| Target `/healthz` SHA | Per tick + per replay | Detect target_version changes; record replay's `target_version` |
| `config/regression.yaml` | Every tick | Cadence per category, parallelism limits |

---

## 3. Outputs

Three write surfaces, all narrowly scoped:

- **`regression_schedule`** — UPDATE `last_run_at`, `last_verdict`,
  `last_target_version`, append to `target_versions_passed` on every
  replay. INSERT new rows on every FAIL fan-out (per
  `ARCHITECTURE.md` §4 step 7c(b)).
- **`vulnerabilities.state`** — transition writes for `fix_validated`
  and `reopened` only (per ARCHITECTURE.md §13). No other state
  writes from this component.
- **`attack_queue`** — INSERT new rows with `source='regression'`
  to schedule replays (per §4). The harness enqueues replays; it
  **never writes `attack_runs` directly** — the dispatcher
  (`docs/components/attack-queue.md` §5) owns that INSERT.

The harness does **not** write `vuln_reports` (Documentation Agent
owns that) and does **not** write `attack_runs` directly. It reads
`attack_runs` (after the dispatcher INSERTs and the Judge UPDATEs
the verdict) to decide on state transitions.

**Replay behavior for closed vulns:** the harness DOES continue
replaying `regression_schedule` rows whose vuln is in
`state='closed'` — a closed fix is still a fix that needs validation,
and a re-FAIL on a closed vuln is precisely what should trigger a
`reopened` transition. The only way to stop replays is to explicitly
set `regression_schedule.enabled = false` from the dashboard (see §8).

---

## 4. Replay flow — one attack end-to-end

```
   ┌─────────────────────────────────┐
   │  Trigger: cron tick OR          │
   │           target_version change │
   └─────────────────┬───────────────┘
                     │
                     ▼
   ┌─────────────────────────────────┐
   │  SELECT FROM regression_schedule │
   │  WHERE enabled = true            │
   │    AND ( last_run_at IS NULL     │
   │          OR last_run_at +        │
   │             rerun_interval_days  │
   │             < now() )            │
   └─────────────────┬───────────────┘
                     │ batch of due rows
                     ▼
   ┌──────────────────────────────────┐
   │  For each row, in parallel        │
   │  (capped by max_parallel):        │
   │                                   │
   │   1. Load original attack_runs    │
   │      row by attack_run_id         │
   │   2. ENQUEUE attack_queue row:    │
   │        source='regression'        │
   │        parent_id=<original>       │
   │        + prompt + cat + channel   │
   │        + harness_version metadata │
   │      (harness never writes        │
   │       attack_runs directly)       │
   │   3. Dispatcher consumes the      │
   │      queue entry → executes       │
   │      against live target →        │
   │      INSERTs attack_runs row      │
   │   4. Judge UPDATEs verdict on     │
   │      the dispatcher's row         │
   │   5. Harness watches for newly    │
   │      judged source='regression'   │
   │      rows whose parent_id ties    │
   │      back to a schedule entry,    │
   │      then UPDATEs                 │
   │      regression_schedule with     │
   │      new verdict + version        │
   └─────────────────┬────────────────┘
                     │
                     ▼
   ┌─────────────────────────────────┐
   │  Compare new verdict to original │
   │  (which was always FAIL when     │
   │   first promoted). Three cases:  │
   │                                  │
   │   PASS  → if ALL linked variants │
   │           also PASS:             │
   │           vuln → fix_validated   │
   │                                  │
   │   PARTIAL → no state change;     │
   │             log soft signal       │
   │                                  │
   │   FAIL  → if vuln was            │
   │           closed/validated:      │
   │           vuln → reopened        │
   │           + alert in dashboard   │
   └──────────────────────────────────┘
```

Each replay produces a *new* `attack_runs` row (never overwrites the
original) — created by the dispatcher, not the harness. The lineage
is preserved via `parent_id`. So a vuln's "history" is reconstructible
by walking `attack_runs` for all rows where `parent_id` chains back
to the originating attack_run.

---

## 5. Fix validation logic — only when EVERY linked attack passes

A vuln with `parent_vuln_id` chains (i.e., one root vuln + N
class-probe variants) is fix-validated atomically. Rules:

1. The originating attack replays as `PASS` on the new target_version.
2. **All** vulns with `parent_vuln_id = <root_vuln_id>` also replay as
   `PASS` (i.e., every variant the class-probe found).
3. **No** cross-category regression (see §6) appeared during the
   sweep that triggered this validation.

When all 3 hold, the harness writes:

```sql
UPDATE vulnerabilities
   SET state = 'fix_validated',
       state_changed_at = now(),
       state_changed_by = 'regression_harness',
       last_seen_version = <new_target_version>
 WHERE id = <root_vuln_id>;
```

If any linked attack still FAILs, the root vuln stays in
`fix_proposed`. Each variant's individual row updates to `PASS` in
`target_versions_passed` but the root doesn't transition until the
whole class is clean.

---

## 6. Cross-regression detection — the "did fixing A break B?" check

When a new `target_version` is detected, the harness triggers a **full
sweep** rather than the partial weekly replay. The sweep:

1. Queries every row in `regression_schedule` (enabled or not — see §8
   for "not-enabled" semantics).
2. Replays each in parallel under `max_parallel_sweep` (config; default
   10 to avoid hammering the target).
3. Compares the new verdict against `target_versions_passed`:
   - If a vuln's `target_versions_passed` list previously included a
     version, AND the new replay FAILs, **that vuln has reopened on
     this version even though it was fixed on a prior one**.
4. Logs each such case to `cross_regressions` table:
   ```
   cross_regressions:
     - vuln_id (the one that re-failed)
     - prior_passed_version (when fix held)
     - current_version (where it now fails)
     - triggering_deploy_id (which deploy introduced the regression)
   ```
5. Updates the affected `vulnerabilities.state` to `reopened`.

This is the platform's answer to the PRD's *"a fix that breaks
something else is worse than no fix at all"* concern. Cross-regressions
are surfaced prominently in the Vuln Board with a distinct severity
badge.

---

## 7. Scheduling — Postgres-driven, no external cron

The harness uses **Postgres as the queue**, not an external scheduler
(no Celery, no Airflow). Two reasons:

- The state (which rows are due, last_run_at, etc.) lives in Postgres
  already — adding a parallel scheduler creates two sources of truth.
- The platform's small enough that a single poller-loop suffices.

The poller:

```python
async def regression_poller():
    while True:
        due = await select_due_rows(now=utcnow())
        if due:
            await dispatch_replays(due[:MAX_PARALLEL])
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
```

- `POLL_INTERVAL_SECONDS` default: 300 (5 min). Tight enough to
  service overdue replays promptly; loose enough not to hammer the DB.
- `MAX_PARALLEL` default: 10. Caps concurrent replays so the target
  isn't overwhelmed.
- The poller is a single LangGraph node invoked on a cron loop, not
  the main FastAPI request thread — runs in its own process / worker.

A separate listener watches the target's `/healthz` for SHA changes
(polled every 60s in production). On SHA change, it enqueues a
**sweep job** in `regression_schedule` with priority that ensures all
rows replay against the new version before the next periodic poll.

---

## 8. Disabled rows — the "stop monitoring this vuln" semantics

A `regression_schedule.enabled = false` row stays in the table but is
skipped by both the periodic poller and the full sweep. Use cases:

- Vuln explicitly closed and acknowledged unrecoverable by the operator
  (e.g., "we won't fix this; it's accepted risk").
- Test fixture or false positive that we no longer want polluting the
  regression queue.

A disabled row can be re-enabled at any time via the dashboard. Its
historical `target_versions_passed` array is preserved — useful for
audit "what did we know about this vuln in version 0.4.1?"

Closed vulns (state=`closed`) do NOT automatically disable their
`regression_schedule` row. Closed vulns continue to be regression-
replayed because the platform's bet is *"a closed fix is still a fix
that needs validation."* Disabling is an explicit operator action.

---

## 9. Operational defaults

| Concern | Default |
|---|---|
| Poll interval | 300 seconds (5 min) |
| Max parallel replays per tick | 10 |
| Default rerun interval | 7 days |
| `/healthz` SHA poll interval | 60 seconds (prod) / 300 seconds (MVP) |
| Sweep priority | Always preempts the periodic queue when SHA change detected |
| Idempotency | Each replay produces a new `attack_runs` row; a duplicate trigger on the same `(schedule_id, target_version)` is a no-op |
| Retry | If the target is unreachable, retry the replay 3× over 1 hour; if still failing, mark `last_run_at = now()` but leave `last_verdict = NULL` + alert dashboard |
| Failure mode | A replay that errors does NOT advance `target_versions_passed`. The vuln stays in its prior state |
| Cost | ~$0.02-0.05 per replay (one attack dispatch + one Judge call); per-vuln annual cost = ~$3-7 with weekly cadence |

---

## 10. Trust boundaries

The harness has authority to write exactly two states:

- `vulnerabilities.state = 'fix_validated'` — when the mechanical
  check (§5) passes
- `vulnerabilities.state = 'reopened'` — when a closed/validated vuln
  re-FAILs

No other writes. The harness never writes `vuln_reports`, never
INSERTs or UPDATEs `attack_runs` directly (the dispatcher INSERTs;
the Judge UPDATEs Judge columns), and never auto-disables
`regression_schedule` rows (operator authority only).

When a vuln transitions to `reopened`, the harness emits an alert
visible in the Vuln Board (high-priority badge) and triggers the
Documentation Agent to UPDATE the `vuln_reports.regression_history`
field with the new reopening event. The Documentation Agent's
`state` write authority does not extend to this transition — only the
harness writes `state='reopened'`.

---

## 11. Versioning

`app/components/regression/__init__.py`:

```python
HARNESS_VERSION = "0.4.0"
```

Bumped on changes to the replay flow, sweep logic, or state-
transition rules. The harness attaches `HARNESS_VERSION` to each
enqueued `attack_queue` row's metadata; the dispatcher copies that
metadata into `attack_runs.harness_version` at INSERT time. The
schema's `attack_runs_harness_version_source_match` CHECK constraint
(`docs/components/database-schema.md` §1) enforces that
`harness_version` is set if and only if `source='regression'`, so
the dispatcher cannot accidentally drop the value or leak it onto
non-regression rows.

---

## 12. Implementation pointers

- Poller: `app/components/regression/poller.py` (the `while True`
  loop)
- Sweep logic: `app/components/regression/sweep.py`
- Replay dispatcher: `app/components/regression/replay.py`
- Cross-regression detector:
  `app/components/regression/cross_regression.py`
- Tests:
  - Replay produces a new `attack_runs` row with `parent_id` set
  - Fix-validation requires ALL linked variants to PASS
  - Cross-regression detection triggers on prior-PASSED + now-FAIL
  - Disabled rows are skipped
  - SHA change triggers sweep that preempts periodic queue
  - Idempotency: duplicate trigger on same `(schedule_id, version)` no-ops
