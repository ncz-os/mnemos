-- MariaDB mirror of #2 — backfill memory_versions.group_id for versioned
-- visibility widening.
--
-- Mirrors mnemos/db_migrations/migrations/0048_memory_versions_group_id.sql.
-- MariaDB has IF NOT EXISTS / IF EXISTS clauses for ALTER TABLE ADD COLUMN
-- since 10.0.2; we use the conditional form here. The migration is also
-- idempotent under repeated application because the conditional skip
-- matches the pre-existing column case.

-- 1. Idempotent column addition.
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME   = 'memory_versions'
     AND COLUMN_NAME  = 'group_id'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE memory_versions ADD COLUMN group_id VARCHAR(64) CHARACTER SET ascii',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2. Backfill from the live memories table. Same rationale as Postgres:
-- we use the live memory's CURRENT group_id because historical group_id
-- is not recoverable. Documented in KNOWN_LIMITATIONS.
UPDATE memory_versions mv
   INNER JOIN memories m ON mv.memory_id = m.id
   SET mv.group_id = m.group_id
 WHERE mv.group_id IS NULL
   AND m.group_id IS NOT NULL;

-- 3. Composite (memory_id, group_id) index. The MariaDB IF NOT EXISTS
-- for CREATE INDEX has shipped since 10.0.1.
SET @idx_exists := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = DATABASE()
     AND TABLE_NAME   = 'memory_versions'
     AND INDEX_NAME   = 'idx_mv_memory_id_group_id'
);
SET @sql := IF(@idx_exists = 0,
  'CREATE INDEX idx_mv_memory_id_group_id ON memory_versions (memory_id, group_id)',
  'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
