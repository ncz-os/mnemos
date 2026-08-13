-- Forward-only lifecycle worker migration. Re-running is safe because Oracle
-- schema provisioning treats ORA-01430 duplicate-column errors as benign.
ALTER TABLE deletion_requests MODIFY (memory_id NULL);
ALTER TABLE deletion_requests ADD target_user_id VARCHAR2(256);
ALTER TABLE deletion_requests ADD target_namespace VARCHAR2(256);
ALTER TABLE deletion_requests ADD confirmed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE deletion_requests ADD notes CLOB;
ALTER TABLE deletion_requests ADD soft_deleted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE deletion_requests ADD restore_by TIMESTAMP WITH TIME ZONE;
ALTER TABLE deletion_requests ADD hard_deleted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE deletion_requests ADD restored_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE memory_archive ADD archived_by VARCHAR2(256) DEFAULT 'system:persephone';
ALTER TABLE memory_archive ADD compressed_content BLOB;
ALTER TABLE memory_archive ADD compression_algo VARCHAR2(32) DEFAULT 'zstd';
ALTER TABLE memory_archive ADD original_size_bytes NUMBER(19);
ALTER TABLE memory_archive ADD compressed_size_bytes NUMBER(19);
ALTER TABLE memory_archive ADD schema_version NUMBER(10) DEFAULT 1;

ALTER TABLE deletion_log ADD content_hash VARCHAR2(64);
ALTER TABLE deletion_log ADD owner_id VARCHAR2(256);
ALTER TABLE deletion_log ADD namespace VARCHAR2(256);
ALTER TABLE deletion_log ADD requested_by VARCHAR2(256);
ALTER TABLE deletion_log ADD request_kind VARCHAR2(32);
ALTER TABLE deletion_log ADD reason CLOB;
ALTER TABLE deletion_log ADD source CLOB;

CREATE INDEX idx_deletion_requests_claim
    ON deletion_requests(status, confirmed_at, requested_at);
