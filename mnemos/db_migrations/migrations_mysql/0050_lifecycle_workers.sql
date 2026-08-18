-- Idempotent forward migration for MySQL/MariaDB lifecycle worker state.
-- Runtime provisioning applies equivalent CREATE TABLE IF NOT EXISTS DDL from
-- mnemos.persistence.mysql. This file is provided for controlled DBA rollout.
--
-- Every id / *_id column here is declared CHARACTER SET ascii to match
-- mnemos.persistence.mariadb, which declares `id VARCHAR(64) CHARACTER SET
-- ascii` throughout. MySQL requires a foreign key and its referent to share a
-- charset AND collation; without the explicit clause these columns inherit the
-- database default (utf8mb4 on the fleet), and the FK below fails with
--
--   errno: 150 "Foreign key constraint is incorrectly formed"
--
-- Measured on a live MariaDB 11 host: the 6.1 migration aborted at
-- CREATE TABLE memory_archive and the service could not start at all.
CREATE TABLE IF NOT EXISTS deletion_requests (
    id VARCHAR(64) CHARACTER SET ascii NOT NULL DEFAULT (UUID()), target_user_id VARCHAR(256) NOT NULL,
    target_namespace VARCHAR(256), requested_by VARCHAR(256) NOT NULL,
    requested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), confirmed_at DATETIME(6),
    status VARCHAR(32) NOT NULL DEFAULT 'requested', notes TEXT, soft_deleted_at DATETIME(6),
    restore_by DATETIME(6), hard_deleted_at DATETIME(6), restored_at DATETIME(6),
    PRIMARY KEY (id), INDEX idx_deletion_requests_claim (status, confirmed_at, requested_at)
);
CREATE TABLE IF NOT EXISTS memory_archive (
    id VARCHAR(64) CHARACTER SET ascii NOT NULL, archived_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    archived_by VARCHAR(256) NOT NULL DEFAULT 'system:persephone', compressed_content LONGBLOB NOT NULL,
    compression_algo VARCHAR(32) NOT NULL DEFAULT 'zstd', original_size_bytes BIGINT NOT NULL,
    compressed_size_bytes BIGINT NOT NULL, schema_version INT NOT NULL DEFAULT 1,
    PRIMARY KEY (id), CONSTRAINT fk_memory_archive_memory FOREIGN KEY (id) REFERENCES memories(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS deletion_log (
    id VARCHAR(64) CHARACTER SET ascii NOT NULL,
    memory_id VARCHAR(64) CHARACTER SET ascii NOT NULL,
    content_hash VARCHAR(64) CHARACTER SET ascii NOT NULL,
    owner_id VARCHAR(256), namespace VARCHAR(256), requested_by VARCHAR(256) NOT NULL,
    requested_at DATETIME(6) NOT NULL, executed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    request_kind VARCHAR(32) NOT NULL, reason TEXT, source JSON, PRIMARY KEY (id)
);
