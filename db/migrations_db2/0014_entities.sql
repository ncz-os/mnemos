-- 0014_entities.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE entities (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    kind VARCHAR(50) NOT NULL,
    name VARCHAR(200) NOT NULL,
    attributes CLOB(1M),
    created TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_entities_kind ON entities (kind);
CREATE INDEX idx_entities_name ON entities (name);
