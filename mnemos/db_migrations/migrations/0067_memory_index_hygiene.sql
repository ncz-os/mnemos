-- migration: 0067_memory_index_hygiene
-- purpose:   Postgres index hygiene for the `memories` table (F-10, 2026-10-31).
--
-- Source: Fable federation report (2026-10-31, commit 0073bb4), F-10 PERF db:
--
--   "F-10 PERF db: Postgres index hygiene: `(updated, id)` partial on
--    `deleted_at IS NULL` (SQLite has it, `migrations_sqlite/migrations.sql:53`;
--    Postgres keyset paths `postgres.py:4813-4845`, `:4954-5133` have none),
--    `(namespace, created DESC)` partial, drop prefix-redundant
--    `idx_memories_owner_id`, cap raw `limit` on sessions history
--    (`routes/sessions.py:268`) and federation log
--    (`routes/federation.py:559`) (S)."
--
-- Re-verified against master 68d5891a: the three index findings
-- all still held.
--
-- 1. `(updated, id)` partial index.
--    Postgres had NO index supporting the `(updated, id)` keyset scan.
--    The federation feed path paginates with
--        WHERE deleted_at IS NULL
--          AND (updated > $1 OR (updated = $1 AND id > $2))
--        ORDER BY updated ASC, id ASC
--    which had to sort the whole surviving corpus on every page.
--    SQLite already carries `idx_memories_updated_id ON memories(updated ASC,
--    id ASC)`; Oracle and Db2 carry `idx_memories_updated_cursor ON
--    memories(deleted_at, updated, id)`. Postgres was the only backend
--    without one.
--
--    The index is PARTIAL on `deleted_at IS NULL` because every keyset
--    path filters that predicate, and soft-deleted rows are the
--    minority of a live corpus -- a partial index keeps the index small
--    and hot.
--
-- 2. `(namespace, created DESC)` partial index.
--    Postgres had `idx_memories_namespace ON memories(namespace)` --
--    namespace alone, with no ordering column. Every namespace-scoped
--    listing orders by `created DESC` (`list_memories`,
--    `fts_search`'s ILIKE fallback, the admin stats path), so the
--    planner had to sort the namespace's rows on every call. The
--    composite index serves the filter AND the ordering.
--
-- 3. Drop the prefix-redundant `idx_memories_owner_id`.
--    `idx_memories_owner_cat ON memories(owner_id, category)` already
--    has `owner_id` as its leading column, so a `(owner_id)` lookup is
--    served by that index -- a separate single-column index on the
--    same leading column is pure write amplification (every INSERT and
--    every UPDATE of `owner_id` must maintain both).
--
--    The drop is idempotent (`IF EXISTS`) so the migration is safe to
--    replay. Postgres' migration runner replays every file at every
--    startup (there is no applied-state ledger yet -- see F-11), so
--    every statement in this file MUST be replay-safe.

-- 1. Keyset pagination support: (updated, id) for the live corpus.
CREATE INDEX IF NOT EXISTS idx_memories_updated_id
    ON memories(updated ASC, id ASC)
    WHERE deleted_at IS NULL;

-- 2. Namespace-scoped listings: filter by namespace, order by recency.
CREATE INDEX IF NOT EXISTS idx_memories_namespace_created
    ON memories(namespace, created DESC)
    WHERE deleted_at IS NULL;

-- 3. Drop the prefix-redundant single-column owner_id index.
--    idx_memories_owner_cat ON memories(owner_id, category) already
--    covers owner_id lookups as its leading column.
DROP INDEX IF EXISTS idx_memories_owner_id;
