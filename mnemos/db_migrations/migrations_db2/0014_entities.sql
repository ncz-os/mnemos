-- 0014_entities.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE entities (
    id VARCHAR(36) PRIMARY KEY,
    kind VARCHAR(50) NOT NULL,
    name VARCHAR(200) NOT NULL,
    attributes CLOB(1M),
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_entities_kind ON entities (kind);
CREATE INDEX idx_entities_name ON entities (name);
