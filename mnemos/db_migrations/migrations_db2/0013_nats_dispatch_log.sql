-- 0013_nats_dispatch_log.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE nats_dispatch_log (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    subject VARCHAR(200) NOT NULL,
    payload CLOB(1M),
    published_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP NOT NULL,
    acked_at TIMESTAMP(6)
);

CREATE INDEX idx_nats_dispatch_subject ON nats_dispatch_log (subject);
CREATE INDEX idx_nats_dispatch_published ON nats_dispatch_log (published_at);
