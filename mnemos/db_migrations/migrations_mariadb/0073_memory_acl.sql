-- MySQL/MariaDB mirror of 0043_memory_acl — per-principal ACL escape hatch.
--
-- Until this table existed, the MySQL/MariaDB visibility predicate had no
-- ACL EXISTS disjunct (documented parity gap). Version-history reads now
-- render the disjunct against this table, exactly like Postgres / SQLite /
-- Oracle / Db2.
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
CREATE TABLE IF NOT EXISTS memory_acl (
  memory_id  VARCHAR(256) NOT NULL,
  principal  VARCHAR(512) NOT NULL,
  perm       INT NOT NULL DEFAULT 4,
  granted_by VARCHAR(256),
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (memory_id, principal),
  CONSTRAINT fk_memory_acl_memory FOREIGN KEY (memory_id)
      REFERENCES memories(id) ON DELETE CASCADE,
  CONSTRAINT chk_memory_acl_principal CHECK (principal LIKE 'user:%' OR principal LIKE 'group:%'),
  CONSTRAINT chk_memory_acl_perm CHECK (perm >= 0 AND perm <= 7)
);

CREATE INDEX IF NOT EXISTS idx_memory_acl_principal ON memory_acl(principal);
