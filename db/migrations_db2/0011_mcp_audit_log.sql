-- 0011_mcp_audit_log.sql — Db2 12.1.5 (Oracle Compat) port.

CREATE TABLE mcp_audit_log (
    id VARCHAR(36) PRIMARY KEY,
    session_id VARCHAR(36),
    tool_name VARCHAR(100) NOT NULL,
    request CLOB(1M),
    response CLOB(1M),
    duration_ms INTEGER,
    created TIMESTAMP(6) WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE INDEX idx_mcp_audit_log_session ON mcp_audit_log (session_id);
CREATE INDEX idx_mcp_audit_log_tool ON mcp_audit_log (tool_name);
CREATE INDEX idx_mcp_audit_log_created ON mcp_audit_log (created);
