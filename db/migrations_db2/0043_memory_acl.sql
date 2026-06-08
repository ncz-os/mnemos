-- 0043_memory_acl.sql — Db2 12.1.5 (Oracle Compat) port.
--
-- Per-principal ACL escape hatch + delegated group-admin. RLS is not used on
-- Db2; the application visibility predicate (shared _render_visibility from
-- mnemos.persistence.oracle, used by the Db2 backend) is the enforcement
-- boundary and reads memory_acl via an EXISTS disjunct. principal is
-- 'user:<id>' or 'group:<id>'; perm is a Unix-style bitmask (read=4, write=2).
-- A grant only widens visibility on top of the memory's own mode bits.

-- Idempotent CREATE TABLE (42710 = object already exists). Inner single
-- quotes in CHECK constraints are doubled for EXECUTE IMMEDIATE.
BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
    EXECUTE IMMEDIATE '
        CREATE TABLE memory_acl (
            memory_id  VARCHAR(100) NOT NULL,
            principal  VARCHAR(150) NOT NULL,
            perm       SMALLINT DEFAULT 4 NOT NULL,
            granted_by VARCHAR(100),
            created    TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
            CONSTRAINT pk_memory_acl PRIMARY KEY (memory_id, principal),
            CONSTRAINT fk_memory_acl_memory FOREIGN KEY (memory_id)
                REFERENCES memories (id) ON DELETE CASCADE,
            CONSTRAINT ck_memory_acl_principal
                CHECK (principal LIKE ''user:%'' OR principal LIKE ''group:%''),
            CONSTRAINT ck_memory_acl_perm CHECK (perm BETWEEN 0 AND 7)
        )';
END@

-- Idempotent CREATE INDEX (42710 = object already exists).
BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42710' BEGIN END;
    EXECUTE IMMEDIATE 'CREATE INDEX idx_memory_acl_principal ON memory_acl (principal)';
END@

-- Delegated group-admin flag. Idempotent via 42711 continue handler (0041 convention).
BEGIN
    DECLARE CONTINUE HANDLER FOR SQLSTATE '42711' BEGIN END;
    EXECUTE IMMEDIATE 'ALTER TABLE user_groups ADD COLUMN is_admin SMALLINT DEFAULT 0 NOT NULL';
END@
