-- 0007_groups.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE groups (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description CLOB(1M),
    created TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_groups_name ON groups (name);
