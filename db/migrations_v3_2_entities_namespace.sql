-- MNEMOS v3.2 — per-entity namespace column
--
-- Entities gained `owner_id` in migrations_v3_ownership.sql but not
-- `namespace`. Now that per-user namespaces are live
-- (migrations_v3_2_user_namespace.sql), entities need the same
-- two-dimensional tenancy gate as memories/kg/dag/webhooks.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + DEFAULT populates existing
-- rows with 'default' in place — no separate backfill step needed
-- because entities have no linked memory to inherit from.

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT 'default';

-- Indexes matching the owner_id patterns above it.
CREATE INDEX IF NOT EXISTS idx_entities_namespace
    ON entities(namespace);
CREATE INDEX IF NOT EXISTS idx_entities_owner_namespace
    ON entities(owner_id, namespace);

-- Note: the (owner_id, entity_type, name) UNIQUE constraint stays
-- as-is. Namespace segmentation WITHIN a single owner (same owner
-- having "Alice" the person in namespace A and namespace B) is not
-- supported today; tenant separation is expressed via distinct
-- owner_ids per team. Widening the unique key is a v3.3+ decision
-- that also needs migration discipline for rows created before the
-- change.
