-- 0014_entities.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE entities (
    id VARCHAR2(36) PRIMARY KEY,
    kind VARCHAR2(50) NOT NULL,
    name VARCHAR2(200) NOT NULL,
    attributes CLOB CHECK (attributes IS JSON),
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_entities_kind ON entities (kind);
CREATE INDEX idx_entities_name ON entities (name);
