-- SQLite mirror of v6.3 — per-principal ACL escape hatch + delegated group-admin.
-- RLS is not available in SQLite; the application visibility predicate in
-- mnemos.core.visibility / mnemos.persistence.sqlite is the enforcement
-- boundary. The ACL EXISTS disjunct there reads this table.
--
-- principal is 'user:<id>' or 'group:<id>'. perm is a Unix-style bitmask
-- (read=4, write=2). A grant only ever widens visibility on top of the
-- memory's own mode bits.
CREATE TABLE IF NOT EXISTS memory_acl (
  memory_id  TEXT    NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  principal  TEXT    NOT NULL
             CHECK (principal LIKE 'user:%' OR principal LIKE 'group:%'),
  perm       INTEGER NOT NULL DEFAULT 4
             CHECK (perm >= 0 AND perm <= 7),
  granted_by TEXT,
  created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (memory_id, principal)
);

CREATE INDEX IF NOT EXISTS idx_memory_acl_principal ON memory_acl(principal);

-- Delegated group-admin flag. The migration loader treats a re-run
-- "duplicate column name" as a no-op, so this stays idempotent.
ALTER TABLE user_groups ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;
