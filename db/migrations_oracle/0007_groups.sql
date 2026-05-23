-- 0007_groups.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE groups (
    id VARCHAR2(36) PRIMARY KEY,
    name VARCHAR2(100) NOT NULL UNIQUE,
    description CLOB,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_groups_name ON groups (name);
