-- migrations/005_cost_rollup.sql
-- Adds campaign-level cost + token columns so the Documentation Agent's
-- cost rollup view + dashboard cost panel can attribute LLM spend.
--
-- Cost is *attributed to the red-team side only* — i.e. the OpenAI bill
-- the harness incurs for Judge + mutator + class-probe + partial-reentry
-- LLM calls. The target's own LLM spend is the target operator's problem
-- and is not visible to us through a black-box chat interface.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS so re-running on a populated DB is
-- safe.

ALTER TABLE campaigns
  ADD COLUMN IF NOT EXISTS total_cost_usd  NUMERIC(10,4) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_tokens_in  BIGINT       NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_tokens_out BIGINT       NOT NULL DEFAULT 0;
