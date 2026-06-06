-- 0043_memory_acl.sql — Oracle 23ai port for MNEMOS parity.
--
-- Per-principal ACL escape hatch + delegated group-admin. RLS is not used on
-- Oracle; the application visibility predicate in mnemos.persistence.oracle
-- (_render_visibility) is the enforcement boundary and reads memory_acl via an
-- EXISTS disjunct. principal is 'user:<id>' or 'group:<id>'; perm is a
-- Unix-style bitmask (read=4, write=2). A grant only widens visibility on top
-- of the memory's own mode bits.

CREATE TABLE memory_acl (
    memory_id  VARCHAR2(100) NOT NULL,
    principal  VARCHAR2(150) NOT NULL,
    perm       NUMBER(3) DEFAULT 4 NOT NULL,
    granted_by VARCHAR2(100),
    created    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_memory_acl PRIMARY KEY (memory_id, principal),
    CONSTRAINT fk_memory_acl_memory FOREIGN KEY (memory_id)
        REFERENCES memories (id) ON DELETE CASCADE,
    CONSTRAINT ck_memory_acl_principal
        CHECK (principal LIKE 'user:%' OR principal LIKE 'group:%'),
    CONSTRAINT ck_memory_acl_perm CHECK (perm BETWEEN 0 AND 7)
);

CREATE INDEX idx_memory_acl_principal ON memory_acl (principal);

-- Delegated group-admin flag.
ALTER TABLE user_groups ADD is_admin NUMBER(1) DEFAULT 0 NOT NULL;
