-- MNEMOS v3.2 — per-user namespace column
--
-- Codex memory-OS audit (019dbd11) flagged that UserContext.namespace
-- was sourced from one global config default, not per-user state.
-- Every two-dimensional tenancy gate shipped in v3.1.2 therefore
-- collapsed to one-dimensional in practice (owner_id alone) on any
-- multi-user install that used the default config.
--
-- This migration adds a per-user `namespace` column. auth.py reads
-- the column into UserContext so the tenancy gate is actually
-- per-user.
--
-- Idempotent by construction: ADD COLUMN IF NOT EXISTS with a DEFAULT
-- so existing rows are populated immediately (no separate backfill
-- step needed — the DEFAULT matches the legacy global default, so
-- pre-migration behavior is preserved bit-for-bit for existing
-- single-namespace installs).

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

-- Index for common admin query: "list every user in namespace X".
CREATE INDEX IF NOT EXISTS idx_users_namespace ON users(namespace);
