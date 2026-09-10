-- MCP OAuth 2.1 DCR, PKCE, refresh-family and persisted signing-key storage.
CREATE TABLE IF NOT EXISTS oauth_mcp_clients (
    client_id TEXT PRIMARY KEY,
    client_secret TEXT,
    redirect_uris TEXT NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS oauth_mcp_authorization_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    code_challenge TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_mcp_codes_client ON oauth_mcp_authorization_codes(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_mcp_codes_expires ON oauth_mcp_authorization_codes(expires_at);
CREATE TABLE IF NOT EXISTS oauth_mcp_tokens (
    jti TEXT PRIMARY KEY,
    refresh_token_hash TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    family_id TEXT NOT NULL,
    parent_jti TEXT,
    replaced_by_jti TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_mcp_tokens_client ON oauth_mcp_tokens(client_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oauth_mcp_tokens_hash ON oauth_mcp_tokens(refresh_token_hash);
CREATE INDEX IF NOT EXISTS idx_oauth_mcp_tokens_family ON oauth_mcp_tokens(family_id);
CREATE TABLE IF NOT EXISTS oauth_mcp_signing_keys (
    key_id TEXT PRIMARY KEY,
    signing_key TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    rotated_at TEXT
);
