-- 0013_nats_dispatch_log.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE nats_dispatch_log (
    id VARCHAR2(36) PRIMARY KEY,
    subject VARCHAR2(200) NOT NULL,
    payload CLOB CHECK (payload IS JSON),
    published_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    acked_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_nats_dispatch_subject ON nats_dispatch_log (subject);
CREATE INDEX idx_nats_dispatch_published ON nats_dispatch_log (published_at);
