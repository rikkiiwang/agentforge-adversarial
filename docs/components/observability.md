# Observability (Langfuse)

**Status:** Design-of-record, draft 2026-05-12. Referenced from
`ARCHITECTURE.md` §5 ("Observability" comms layer).

---

## 1. Purpose

Make every agent decision, every LLM call, and every state transition
**replayable** in a third-party tool that's independent of the
platform's own database. Two responsibilities:

- **Technical observability** — per-agent traces, token / cost spans,
  inter-agent message hand-offs, replay-by-vuln-id. The substrate the
  operator (and the Orchestrator's drift detector) uses to ask "what
  exactly happened on 2026-05-15?"
- **Decoupling from Postgres** — Langfuse is a separate concern from
  the platform's source of truth. If Postgres is unavailable, the
  agents emit Langfuse traces anyway (degraded mode); if Langfuse is
  unavailable, agents continue writing Postgres.

This file's design borrows directly from the W2 pattern (per
`memory-bank/systemPatterns.md` B10) — split logging where the
technical trace ≠ the audit log. Same principle, applied here at the
multi-agent layer instead of the per-tool layer.

---

## 2. Trace shape — one trace per campaign

The **trace** is the unit of observability. One Langfuse trace per
`campaigns.id`:

```
trace: campaign-{campaign_id}
├── span: orchestrator_dispatch                  [Pure-Python, no LLM]
│   └── metadata: scoring_breakdown, picked_cell, budget_usd
├── span: red_team_swarm                          [parallel fan-out]
│   ├── span: subagent-llama        (generation) [Ollama]
│   │   └── metadata: model, prompt_template, mode, schema_retries
│   ├── span: subagent-deepseek     (generation) [Ollama]
│   └── span: subagent-mistral      (generation) [Ollama]
├── span: synthesize_fn                           [Pure-Python, no LLM]
│   └── metadata: candidates_in, candidates_out, dedup_clusters, novelty_drops
├── span: target_dispatch_k_attacks               [N child spans]
│   ├── span: attack_runs/<id>      (HTTPS to target)
│   ├── span: attack_runs/<id>      (HTTPS to target)
│   └── ...
├── span: judge                                   [per attack_run]
│   ├── span: judge_haiku           (generation) [Anthropic]
│   ├── span: judge_sonnet          (generation) [Anthropic; if ensemble]
│   └── metadata: rubric_version, predicates, triggered_rule, verdict
└── span: orchestrator_postverdict                [Pure-Python, no LLM]
    └── metadata: routing_decision (fan_out|near_miss|pass_only)
```

A FAIL also nests a sub-trace under `orchestrator_postverdict`:

```
└── span: documentation_agent       (generation)  [Anthropic Sonnet]
└── span: regression_harness_insert (DB write)
└── span: class_probe_swarm         [recursive Red Team swarm sub-trace]
```

The class-probe sub-trace becomes its own root trace (so it can be
inspected independently) but carries a `trace_metadata.parent_campaign`
link back to the originating campaign trace. Drill-down by clicking
in the Langfuse UI.

---

## 3. Generation spans — model + cost + rubric_version

Every LLM call gets a Langfuse `generation` span:

```python
with langfuse.generation(
    name="judge_haiku",
    model="claude-haiku-4-5",
    input=prompt,
    output=response,
    metadata={
        "rubric_version": RUBRIC_VERSION,
        "category": brief.cell.category,
        "subagent_id": spec.id,
        "schema_retries": 0,
    },
    usage={"input": tokens_in, "output": tokens_out, "total_cost": cost_usd},
) as gen:
    response = await client.messages.create(...)
    gen.update(output=response)
```

This is the W2 pattern from KR3 (per
`memory-bank/systemPatterns.md` B10) — every adapter wraps its
`call()` in a `generation` span so the model identity and cost surface
in the trace. Mandatory on every LLM call in this platform.

---

## 4. PHI minimization — borrowed from W2

Same constraint as the W1/W2 Co-Pilot: **no raw PHI in observability**.
The platform's attack transcripts can contain references to Synthea
demo patients, but the trace ingestion must scrub anything that
matches PHI patterns before it hits Langfuse Cloud.

Implementation: the same `phi/log_filter.py` pattern from the W2
codebase, but tuned for the adversarial platform's surface:

- Stripping pass on every `attack_prompt`, `agent_output`,
  `judge_reasoning`, and `vuln_reports.observed_behavior` field
  before they reach Langfuse.
- Patterns scrubbed: SSN, DOB, real-looking names (first+last), phone,
  address, MRN-style IDs.
- Replacement: deterministic per-trace pseudonym (so traces remain
  reconstructable but external uploaders see no PHI).
- Failure-open default for non-PHI content (don't break tracing on
  pattern uncertainty).

This is the **same defense** the W2 Co-Pilot uses for its own
observability layer — applied here at a layer above the LLM
boundary, before any data leaves the platform process.

---

## 5. Cost rollups — the link to dashboard $/finding

Every `generation` span carries `usage.total_cost`. These accumulate
into:

1. **Per-trace cost** — sum of all generation spans in one campaign.
2. **Per-agent role cost** — sum across all traces for a given role
   (red_team_swarm vs. judge vs. documentation_agent), used by the
   dashboard's cost tile.
3. **Per-category cost** — for the daily-pool tracker (the
   Orchestrator's per-category budget logic from
   `docs/agents/orchestrator.md` §6).

Persisted in a `cost_rollup_daily` materialized view in Postgres
(refreshed every 60s by a trigger on every new `attack_runs` row).
The dashboard reads this view; the Orchestrator reads the same view
when computing budget allocation.

---

## 6. Drift detection feeds — Judge calibration sample

The Judge's weekly `make judge-drift-check` cron (per
`docs/agents/judge.md` §9) queries Langfuse traces directly, not
Postgres. Reason: traces carry the full LLM input/output for re-
judgment, while Postgres only stores the *resulting verdict*.

The cron:
1. Samples 100 traces uniformly across the last week's categories
2. Re-judges each transcript with the current `RUBRIC_VERSION`
3. Compares new verdict to original; counts disagreements
4. Surfaces drift > 5% as a dashboard alert

The trace itself becomes the test fixture — no separate calibration
DB needed.

---

## 7. Operational defaults

| Concern | Default |
|---|---|
| Backend | Langfuse Cloud (same as W1/W2 Co-Pilot) |
| SDK pinning | `langfuse>=2.50,<3` (same as W1/W2 — v3 dropped `Langfuse.trace()` in favor of OTel-style spans; migration deferred) |
| Trace TTL | 90 days (Langfuse Cloud default) |
| Sampling | 100% in MVP; per-category sampling deferred to Final-scope if cost becomes a concern |
| PHI scrub | On every emit, no opt-out |
| Cost tracking | Mandatory on every LLM call |
| Failure mode | Langfuse errors are logged + swallowed; platform continues. A `langfuse_emit_failed` counter feeds the dashboard alerts tile |
| Cost overhead | ~3% per LLM call (network round-trip), $0 ingestion cost on Cloud free tier (up to 50K events/month) |

---

## 8. Span naming convention

Strict naming for reproducibility:

| Span name | Layer | Notes |
|---|---|---|
| `campaign-{campaign_id}` | trace root | One per campaign |
| `orchestrator_dispatch` | top-level span | No LLM child |
| `red_team_swarm` | top-level span | Parent of N subagent generations |
| `subagent-{id}` | generation span | Child of `red_team_swarm` |
| `synthesize_fn` | top-level span | Pure Python; no LLM child |
| `target_dispatch_k_attacks` | top-level span | N child spans, one per attack_run |
| `attack_runs/{id}` | span | HTTPS to target; metadata captures verdict |
| `judge` | top-level span | Parent of judge LLM call(s) |
| `judge_haiku` / `judge_sonnet` | generation span | Child of `judge` |
| `orchestrator_postverdict` | top-level span | Pure Python; routes to fan-out |
| `documentation_agent` | generation span | Child of `orchestrator_postverdict` on FAIL |
| `regression_harness_insert` | span | DB write; no LLM child |
| `class_probe_swarm` | top-level span | Recursive child trace |

This naming is encoded as constants in `app/observability/spans.py`
so typos can't cause span fragmentation.

---

## 9. Versioning

`app/observability/__init__.py`:

```python
OBSERVABILITY_VERSION = "0.4.0"
```

Bumped on schema changes to trace structure or new mandatory metadata
fields. Recorded as `trace_metadata.observability_version` on every
trace so historical replay is reconstructable.

---

## 10. Implementation pointers

- Tracer wrapper: `app/observability/tracer.py` (single shared
  Langfuse client, lifecycle-managed by FastAPI startup hook)
- PHI scrub: `app/observability/phi_scrub.py` (borrowed from W2's
  `copilot/app/phi/log_filter.py`)
- Span constants: `app/observability/spans.py`
- Cost rollup view DDL: `migrations/0005_cost_rollup_daily.sql`
- Generation-span helpers: `app/observability/llm_span.py` (matches
  W2's adapter pattern from KR3)
- Tests:
  - PHI scrub catches SSN / DOB / names / phone patterns
  - Cost rollup correctly aggregates per role + per category
  - Trace span names match constants; no typos
  - Failure-open: Langfuse 500 doesn't break the campaign
  - End-to-end: a campaign produces a complete trace tree
