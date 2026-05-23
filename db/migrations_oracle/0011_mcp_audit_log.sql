-- 0011_mcp_audit_log.sql — Oracle 23ai port for MNEMOS parity.

CREATE TABLE mcp_audit_log (
    id VARCHAR2(36) PRIMARY KEY,
    session_id VARCHAR2(36),
    tool_name VARCHAR2(100) NOT NULL,
    request CLOB CHECK (request IS JSON),
    response CLOB CHECK (response IS JSON),
    duration_ms NUMBER,
    created TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_mcp_audit_log_session ON mcp_audit_log (session_id);
CREATE INDEX idx_mcp_audit_log_tool ON mcp_audit_log (tool_name);
CREATE INDEX idx_mcp_audit_log_created ON mcp_audit_log (created);
