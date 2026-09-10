-- MCP OAuth 2.1 DCR, PKCE, refresh-family and persisted signing-key storage.
-- VARBINARY identifiers enforce exact equality, including trailing whitespace.
-- MySQL/MariaDB VARCHAR binary collations can still use PAD SPACE semantics.
CREATE TABLE IF NOT EXISTS oauth_mcp_clients (
    client_id VARBINARY(255) PRIMARY KEY,
    client_secret VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin,
    redirect_uris JSON NOT NULL,
    token_endpoint_auth_method VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS oauth_mcp_authorization_codes (
    code VARBINARY(255) PRIMARY KEY,
    client_id VARBINARY(255) NOT NULL,
    code_challenge VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    code_challenge_method VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    used_at DATETIME(6),
    FOREIGN KEY (client_id) REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    INDEX idx_oauth_mcp_codes_client (client_id),
    INDEX idx_oauth_mcp_codes_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS oauth_mcp_tokens (
    jti VARBINARY(255) PRIMARY KEY,
    refresh_token_hash VARBINARY(255) NOT NULL,
    client_id VARBINARY(255) NOT NULL,
    family_id VARBINARY(255) NOT NULL,
    parent_jti VARBINARY(255),
    replaced_by_jti VARBINARY(255),
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6),
    FOREIGN KEY (client_id) REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    INDEX idx_oauth_mcp_tokens_client (client_id),
    UNIQUE INDEX idx_oauth_mcp_tokens_hash (refresh_token_hash),
    INDEX idx_oauth_mcp_tokens_family (family_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS oauth_mcp_signing_keys (
    key_id VARBINARY(255) PRIMARY KEY,
    signing_key TEXT NOT NULL,
    created DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    rotated_at DATETIME(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
