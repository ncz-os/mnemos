-- 0015_journal.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE journal (
    id VARCHAR2(36) PRIMARY KEY,
    entity_id VARCHAR2(36) NOT NULL,
    action VARCHAR2(50) NOT NULL,
    before CLOB CHECK (before IS JSON),
    after CLOB CHECK (after IS JSON),
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_journal_entity ON journal (entity_id);
CREATE INDEX idx_journal_created ON journal (created);
