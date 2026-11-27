-- MySQL/MariaDB mirror of 0043_memory_acl — per-principal ACL escape hatch.
--
-- The version-history renderers on MySQL / MariaDB (``_version_visibility_clause``
-- in mysql.py) apply the ACL EXISTS disjunct against this table, exactly like
-- SQLite / Oracle / Db2 / Postgres. Until this table existed, the MySQL
-- version-history predicate had no ACL EXISTS term (documented parity gap).
--
-- principal is 'user:<id>' or 'group:<id>'. perm is a Unix-style bitmask
-- (read=4, write=2). A grant only ever widens visibility on top of the
-- memory's own mode bits.
--
-- FK/CASCADE: column-level REFERENCES is parsed but IGNORED by InnoDB —
-- MySQL/MariaDB only creates a foreign key from a TABLE-LEVEL
-- FOREIGN KEY ... REFERENCES constraint. The explicit table-level FK
-- below is what actually enforces referential integrity and deletes ACL
-- rows when their parent memory is purged.
--
-- The principal lookup index is INLINE (``KEY``), not a standalone
-- ``CREATE INDEX IF NOT EXISTS``: real MySQL 8 (as opposed to MariaDB)
-- does not support ``IF NOT EXISTS`` on CREATE INDEX at all.
-- ``CREATE TABLE IF NOT EXISTS`` already makes the whole table, indexes
-- and constraints included, idempotent — the same convention as
-- 0040_webhook_repository.sql and 0054_memory_tags.sql.
CREATE TABLE IF NOT EXISTS memory_acl (
  memory_id  VARCHAR(256) NOT NULL,
  principal  VARCHAR(512) NOT NULL,
  perm       INT NOT NULL DEFAULT 4,
  granted_by VARCHAR(256),
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (memory_id, principal),
  KEY idx_memory_acl_principal (principal),
  CONSTRAINT fk_memory_acl_memory FOREIGN KEY (memory_id)
      REFERENCES memories(id) ON DELETE CASCADE,
  CONSTRAINT chk_memory_acl_principal CHECK (principal LIKE 'user:%' OR principal LIKE 'group:%'),
  CONSTRAINT chk_memory_acl_perm CHECK (perm >= 0 AND perm <= 7)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
