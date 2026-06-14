-- Cross-backend supersession parity: ensure memories.consolidated_into exists.
-- Postgres base schema generally already has it; IF NOT EXISTS makes this a no-op there.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS consolidated_into VARCHAR(64);
