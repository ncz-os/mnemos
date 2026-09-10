-- The MySQL family previously lacked the common OAuthRepository surface.
-- Binary collation preserves exact principal and token identity.
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(255) PRIMARY KEY,
    display_name TEXT,
    email VARCHAR(320),
    role VARCHAR(32) NOT NULL DEFAULT 'user',
    namespace VARCHAR(255) NOT NULL DEFAULT 'default',
    INDEX idx_oauth_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS user_groups (
    user_id VARCHAR(255) NOT NULL,
    group_id VARCHAR(255) NOT NULL,
    PRIMARY KEY (user_id, group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS api_keys (
    id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    key_hash VARCHAR(128) NOT NULL UNIQUE,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    last_used DATETIME(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS oauth_providers (
    name VARCHAR(100) PRIMARY KEY,
    display_name VARCHAR(255),
    kind VARCHAR(50),
    issuer_url TEXT,
    client_id TEXT,
    client_secret TEXT,
    scope TEXT,
    authorize_url TEXT,
    token_url TEXT,
    userinfo_url TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS oauth_identities (
    id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    provider VARCHAR(100) NOT NULL,
    external_id VARCHAR(512) NOT NULL,
    email VARCHAR(320),
    display_name TEXT,
    raw_claims JSON,
    last_login_at DATETIME(6),
    created DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE INDEX idx_oauth_identity_provider (provider, external_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS oauth_sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    identity_id VARCHAR(255),
    expires_at DATETIME(6) NOT NULL,
    user_agent TEXT,
    ip_address VARCHAR(45),
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at DATETIME(6),
    last_used_at DATETIME(6),
    INDEX idx_oauth_sessions_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
