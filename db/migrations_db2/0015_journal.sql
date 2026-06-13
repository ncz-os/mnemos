-- 0015_journal.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE journal (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    entity_id VARCHAR(36) NOT NULL,
    action VARCHAR(50) NOT NULL,
    before CLOB(1M),
    after CLOB(1M),
    created TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_journal_entity ON journal (entity_id);
CREATE INDEX idx_journal_created ON journal (created);
