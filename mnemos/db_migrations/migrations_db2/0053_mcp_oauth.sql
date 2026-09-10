-- MCP OAuth 2.1 DCR, PKCE, refresh-family and persisted signing-key storage.
CREATE TABLE oauth_mcp_clients (
    client_id VARCHAR(255) NOT NULL PRIMARY KEY,
    client_secret VARCHAR(255),
    redirect_uris CLOB NOT NULL,
    token_endpoint_auth_method VARCHAR(255) NOT NULL,
    created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE oauth_mcp_authorization_codes (
    code VARCHAR(255) NOT NULL PRIMARY KEY,
    client_id VARCHAR(255) NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    code_challenge VARCHAR(255) NOT NULL,
    code_challenge_method VARCHAR(255) NOT NULL,
    redirect_uri CLOB NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP
);
CREATE INDEX idx_oauth_mcp_codes_client ON oauth_mcp_authorization_codes(client_id);
CREATE INDEX idx_oauth_mcp_codes_expires ON oauth_mcp_authorization_codes(expires_at);
CREATE TABLE oauth_mcp_tokens (
    jti VARCHAR(255) NOT NULL PRIMARY KEY,
    refresh_token_hash VARCHAR(255) NOT NULL,
    client_id VARCHAR(255) NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    family_id VARCHAR(255) NOT NULL,
    parent_jti VARCHAR(255),
    replaced_by_jti VARCHAR(255),
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);
CREATE INDEX idx_oauth_mcp_tokens_client ON oauth_mcp_tokens(client_id);
CREATE UNIQUE INDEX idx_oauth_mcp_tokens_hash ON oauth_mcp_tokens(refresh_token_hash);
CREATE INDEX idx_oauth_mcp_tokens_family ON oauth_mcp_tokens(family_id);
CREATE TABLE oauth_mcp_signing_keys (
    key_id VARCHAR(255) NOT NULL PRIMARY KEY,
    signing_key VARCHAR(4000) NOT NULL,
    created TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    rotated_at TIMESTAMP
);
