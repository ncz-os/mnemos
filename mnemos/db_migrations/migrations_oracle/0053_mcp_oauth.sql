-- MCP OAuth 2.1 DCR, PKCE, refresh-family and persisted signing-key storage.
CREATE TABLE oauth_mcp_clients (
    client_id VARCHAR2(255) PRIMARY KEY,
    client_secret VARCHAR2(255),
    redirect_uris CLOB NOT NULL,
    token_endpoint_auth_method VARCHAR2(255) NOT NULL,
    created TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE TABLE oauth_mcp_authorization_codes (
    code VARCHAR2(255) PRIMARY KEY,
    client_id VARCHAR2(255) NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    code_challenge VARCHAR2(255) NOT NULL,
    code_challenge_method VARCHAR2(255) NOT NULL,
    redirect_uri CLOB NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP
);
CREATE INDEX idx_oauth_mcp_codes_client ON oauth_mcp_authorization_codes(client_id);
CREATE INDEX idx_oauth_mcp_codes_expires ON oauth_mcp_authorization_codes(expires_at);
CREATE TABLE oauth_mcp_tokens (
    jti VARCHAR2(255) PRIMARY KEY,
    refresh_token_hash VARCHAR2(255) NOT NULL,
    client_id VARCHAR2(255) NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    family_id VARCHAR2(255) NOT NULL,
    parent_jti VARCHAR2(255),
    replaced_by_jti VARCHAR2(255),
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);
CREATE INDEX idx_oauth_mcp_tokens_client ON oauth_mcp_tokens(client_id);
CREATE UNIQUE INDEX idx_oauth_mcp_tokens_hash ON oauth_mcp_tokens(refresh_token_hash);
CREATE INDEX idx_oauth_mcp_tokens_family ON oauth_mcp_tokens(family_id);
CREATE TABLE oauth_mcp_signing_keys (
    key_id VARCHAR2(255) PRIMARY KEY,
    signing_key VARCHAR2(4000) NOT NULL,
    created TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    rotated_at TIMESTAMP
);
