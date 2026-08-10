--#SET TERMINATOR @
-- Forward-only lifecycle worker migration for Db2.
ALTER TABLE deletion_requests ALTER COLUMN memory_id DROP NOT NULL@
ALTER TABLE deletion_requests ADD COLUMN target_user_id VARCHAR(256)@
ALTER TABLE deletion_requests ADD COLUMN target_namespace VARCHAR(256)@
ALTER TABLE deletion_requests ADD COLUMN confirmed_at TIMESTAMP(6)@
ALTER TABLE deletion_requests ADD COLUMN notes CLOB(1M)@
ALTER TABLE deletion_requests ADD COLUMN soft_deleted_at TIMESTAMP(6)@
ALTER TABLE deletion_requests ADD COLUMN restore_by TIMESTAMP(6)@
ALTER TABLE deletion_requests ADD COLUMN hard_deleted_at TIMESTAMP(6)@
ALTER TABLE deletion_requests ADD COLUMN restored_at TIMESTAMP(6)@

ALTER TABLE memory_archive ADD COLUMN archived_by VARCHAR(256) DEFAULT 'system:persephone'@
ALTER TABLE memory_archive ADD COLUMN compressed_content BLOB(10M)@
ALTER TABLE memory_archive ADD COLUMN compression_algo VARCHAR(32) DEFAULT 'zstd'@
ALTER TABLE memory_archive ADD COLUMN original_size_bytes BIGINT@
ALTER TABLE memory_archive ADD COLUMN compressed_size_bytes BIGINT@
ALTER TABLE memory_archive ADD COLUMN schema_version INTEGER DEFAULT 1@

ALTER TABLE deletion_log ADD COLUMN content_hash VARCHAR(64)@
ALTER TABLE deletion_log ADD COLUMN owner_id VARCHAR(256)@
ALTER TABLE deletion_log ADD COLUMN namespace VARCHAR(256)@
ALTER TABLE deletion_log ADD COLUMN requested_by VARCHAR(256)@
ALTER TABLE deletion_log ADD COLUMN request_kind VARCHAR(32)@
ALTER TABLE deletion_log ADD COLUMN reason CLOB(1M)@
ALTER TABLE deletion_log ADD COLUMN source CLOB(1M)@

CREATE INDEX idx_deletion_requests_claim
    ON deletion_requests(status, confirmed_at, requested_at)@
