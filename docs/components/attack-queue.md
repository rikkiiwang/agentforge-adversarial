# Attack Queue

**Status:** Design-of-record, draft 2026-05-12. Referenced from
`ARCHITECTURE.md` §4 (control flow).

---

## 1. Purpose

A Postgres-backed FIFO that holds **final attacks ready to be
dispatched at the live target**. It exists because:

- The synthesis pipeline can produce K attacks at a faster rate than
  the target can handle in real time.
- Cost caps and target rate limits constrain how many attacks can
  fire per minute / per category — the queue smooths that out.
- Regression replays and class-probe variants need a uniform
  dispatch path; queueing them alongside fresh synthesized attacks
  keeps the dispatch code single-source.

There is **one queue**, not one-per-category or one-per-priority. All
attacks share the same table; priority and rate-limiting are computed
at dequeue time.

---

## 2. Inputs — four sources feeding the queue

| Source | When it writes | Per `ARCHITECTURE.md` |
|---|---|---|
| Synthesis pipeline output | After every Orchestrator dispatch | §4 step 4-5 |
| Mutator (PARTIAL → variants) | On every Judge PARTIAL verdict | §4 step 7 (PARTIAL branch) |
| Class-probe (FAIL → ~10 boundary variants) | On every Judge FAIL verdict | §4 step 7c(c) |
| Regression Harness | Periodic poll + on `target_version` change | `docs/components/regression-harness.md` §4 |

Each entry carries the source tag (`direct`, `random`, `mutator`,
`regression`) so the eventual `attack_runs` row records provenance.

---

## 3. Schema

A dedicated `attack_queue` table (separate from `attack_runs` —
`attack_runs` is the persistent record after dispatch+judge).

**Canonical DDL lives in `docs/components/database-schema.md` §7.**
This file only summarizes the fields conceptually so future readers
don't need to chase the schema doc to understand what each entry
carries.

Per-entry fields:

| Field | Notes |
|---|---|
| `id` | UUID primary key |
| `campaign_id` | FK to `campaigns.id`; groups entries from one Orchestrator dispatch |
| `source` | `direct` / `random` / `mutator` / `regression` (shared `attack_source` enum) |
| `category / subcategory / channel` | Denormalized for fast filter |
| `attack_prompt`, `multi_turn_seq` | The attack payload |
| `parent_id` | FK to `attack_runs.id` for mutator / class-probe / regression sources |
| `priority_score` | See §6 |
| `state` | `queued` / `dispatching` / `dispatched` / `failed` / `cancelled` |
| `enqueued_at`, `dispatched_at` | Lifecycle timestamps |
| `attack_run_id` | FK to `attack_runs.id` after dispatch |
| `failure_reason` | Populated if `state='failed'` |

Postgres uses a partial index on `state='queued'` so the dispatch
poll stays cheap even at 100K+ historical entries.

---

## 4. Dequeue policy

The dispatcher runs as a single async loop:

```python
async def dispatcher_loop():
    while True:
        candidates = await select_dispatch_ready(limit=BATCH_SIZE)
        rate_limited = filter_by_target_rate_limit(candidates)
        cost_capped  = filter_by_per_category_budget(rate_limited)
        for entry in cost_capped:
            asyncio.create_task(dispatch_one(entry))
        await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
```

Two filters between selection and dispatch:

- **`filter_by_target_rate_limit`** — at most `N` concurrent dispatches
  + at most `M` per minute against the same target host. Default `N=5,
  M=20`. Configurable in `config/queue.yaml`.
- **`filter_by_per_category_budget`** — if a category's daily pool is
  exhausted (`docs/agents/orchestrator.md` §6), skip its queue
  entries until the pool refreshes.

Filtered-out entries stay `state='queued'` and re-enter the candidate
set on the next loop tick.

---

## 5. Dispatch flow — `attack_queue` → `attack_runs`

```python
async def dispatch_one(entry):
    await mark_state(entry.id, 'dispatching')
    try:
        transcript = await execute_against_target(entry)
        run_id = await write_attack_run(entry, transcript)
        await mark_state(entry.id, 'dispatched', attack_run_id=run_id)
    except TargetUnreachable as e:
        await mark_state(entry.id, 'failed', failure_reason=str(e))
        await emit_alert('target_unreachable', campaign_id=entry.campaign_id)
```

After the attack_runs row is written, the Judge LangGraph node
picks up the row (separate flow). The queue entry's purpose ends at
`state='dispatched'`.

Queue entries are **not** deleted — they stay for audit. A nightly
job moves entries older than 90 days to a `attack_queue_archive`
table.

---

## 6. Priority computation

`priority_score` is set by the producer (Synthesis, Mutator, Class-
probe, or Regression Harness):

| Source | Score formula |
|---|---|
| Synthesis | `synthesize_fn` score from §5 of synthesis pipeline (already in `[0,1]`) |
| Mutator | Parent PARTIAL's score boost: `min(1.0, parent_score + 0.1)` (slight uplift to keep mutator variants moving) |
| Class-probe | Fixed `0.85` — high but not pre-empting fresh synthesis |
| Regression Harness | Fixed `0.95` — high priority because operator depends on validation latency |

`priority_score DESC` means regression replays generally jump the
queue, but a fresh synthesis with score `1.0` (high novelty + high
coverage gap) can still preempt.

---

## 7. Cancellation

The dispatcher checks for cancellation flags before each dispatch:

- **Budget exhausted mid-campaign** — the dispatcher cancels all
  remaining `queued` entries for that `campaign_id` (sets
  `state='cancelled'`, `failure_reason='budget_exhausted'`).
- **Target version changed mid-campaign** — same: cancel and emit a
  `target_version_changed_mid_campaign` warning. The Regression
  Harness's full-sweep flow takes over to re-test from scratch.
- **Operator manual cancel** — dashboard exposes a button to cancel a
  campaign; flips all `queued` → `cancelled`.

Cancelled entries stay in the table for audit (never deleted) but
never re-enter the dispatch loop.

---

## 8. Operational defaults

| Concern | Default |
|---|---|
| `BATCH_SIZE` (per loop tick) | 20 |
| `DISPATCH_INTERVAL_SECONDS` | 1 second |
| `N` concurrent dispatches per target | 5 |
| `M` dispatches per minute per target | 20 |
| Queue depth alert threshold | 1000 entries queued for >1 hour → dashboard alert |
| Archival age | 90 days → `attack_queue_archive` |
| Failure handling | Failed dispatches are NOT auto-retried; operator can re-queue via dashboard |
| Idempotency | `attack_run_id` foreign key prevents duplicate `attack_runs` from the same queue entry |

---

## 9. Versioning

`app/components/queue/__init__.py`:

```python
QUEUE_VERSION = "0.4.0"
```

Bumped on schema or dispatch-policy changes. Recorded in
`attack_queue.dispatcher_version` per entry (TODO column).

---

## 10. Implementation pointers

- Dispatcher loop: `app/components/queue/dispatcher.py`
- Rate-limit filter: `app/components/queue/rate_limit.py`
- Cost filter: `app/components/queue/cost_filter.py`
- Producer interface (used by Synthesis, Mutator, Class-probe,
  Regression Harness): `app/components/queue/producer.py`
- Tests:
  - Dequeue respects `priority_score DESC`
  - Rate limiter caps concurrent + per-minute dispatches
  - Budget-exhausted cascade cancels all queued entries for the campaign
  - target_version change mid-campaign triggers cancellation
  - Idempotency: re-dispatching the same queue entry is a no-op
