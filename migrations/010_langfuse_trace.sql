-- migrations/010_langfuse_trace.sql
-- ARCHITECTURE.md §5 (Observability): each campaign produces one
-- Langfuse trace with nested per-agent spans and per-LLM-call
-- generations. The trace id is stashed on the campaigns row so the
-- dashboard's campaign drill-down can deep-link directly to the trace
-- in Langfuse Cloud — no separate join table, no extra lookup.
--
-- The column is nullable because:
--   - Pre-existing campaigns rows predate this column.
--   - The harness intentionally degrades to no-op tracing when the
--     LANGFUSE_* env vars are missing (e.g. local dev without an
--     account), so a brand-new campaign can legitimately have no
--     trace id.
--
-- Idempotent — re-running this migration on a populated DB is a no-op.

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS langfuse_trace_id TEXT;

CREATE INDEX IF NOT EXISTS campaigns_langfuse_trace_id_idx
    ON campaigns(langfuse_trace_id)
 WHERE langfuse_trace_id IS NOT NULL;
