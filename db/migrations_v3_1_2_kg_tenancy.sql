-- MNEMOS v3.1.2 — KG triple tenancy
--
-- Brings kg_triples into alignment with the memories tenancy model
-- established in migrations_v1_multiuser.sql: every row carries an
-- owner_id + namespace so the Tier 3 app-layer filters can scope
-- KG reads and writes per-caller.
--
-- Idempotent by construction: ADD COLUMN IF NOT EXISTS, backfill
-- only rows where owner_id IS NULL, CREATE INDEX IF NOT EXISTS.
-- Safe to re-run against a v3.1.2+ database.

-- ---------------------------------------------------------------------------
-- 1. Additive columns on kg_triples
-- ---------------------------------------------------------------------------
ALTER TABLE kg_triples ADD COLUMN IF NOT EXISTS owner_id  TEXT DEFAULT 'default';
ALTER TABLE kg_triples ADD COLUMN IF NOT EXISTS namespace TEXT DEFAULT 'default';

-- ---------------------------------------------------------------------------
-- 2. Backfill: existing v3.1.x rows have no owner / namespace.
--    Inherit from the linked memory when memory_id is set (keeps
--    triple-and-memory ownership aligned for the 99% case where an
--    operator extracts triples from their own memories). Otherwise
--    stamp 'default' like the memories backfill did.
-- ---------------------------------------------------------------------------
UPDATE kg_triples t
SET owner_id  = COALESCE(m.owner_id,  'default'),
    namespace = COALESCE(m.namespace, 'default')
FROM memories m
WHERE t.memory_id = m.id
  AND t.owner_id IS NULL;

UPDATE kg_triples
SET owner_id  = 'default',
    namespace = 'default'
WHERE owner_id IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Set NOT NULL after backfill
-- ---------------------------------------------------------------------------
ALTER TABLE kg_triples ALTER COLUMN owner_id  SET NOT NULL;
ALTER TABLE kg_triples ALTER COLUMN namespace SET NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Indexes — mirror the memories tenancy indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_kg_owner_id      ON kg_triples(owner_id);
CREATE INDEX IF NOT EXISTS idx_kg_namespace     ON kg_triples(namespace);
-- Composite indexes for the common query shape (owner-scoped subject / predicate lookups)
CREATE INDEX IF NOT EXISTS idx_kg_owner_subject ON kg_triples(owner_id, subject);
CREATE INDEX IF NOT EXISTS idx_kg_owner_predicate ON kg_triples(owner_id, predicate);
