-- MNEMOS v5.4.0 migration: provider-neutral OAuth 2.1 authorization server
-- for the remote MCP edge (Claude / ChatGPT / Gemini / etc).
--
-- Single fixed admin subject.  PKCE S256 enforced.  JWT access tokens are
-- signed with an HS256 key stored alongside the rest of the OAuth state.

CREATE TABLE IF NOT EXISTS oauth_mcp_clients (
    client_id                    TEXT PRIMARY KEY,
    client_secret                TEXT,
    redirect_uris                JSONB NOT NULL,
    token_endpoint_auth_method   TEXT NOT NULL DEFAULT 'none',
    created                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS oauth_mcp_authorization_codes (
    code                     TEXT PRIMARY KEY,
    client_id                TEXT NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    code_challenge           TEXT NOT NULL,
    code_challenge_method    TEXT NOT NULL,
    redirect_uri             TEXT NOT NULL,
    expires_at               TIMESTAMPTZ NOT NULL,
    used_at                  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_oauth_mcp_codes_client ON oauth_mcp_authorization_codes(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_mcp_codes_expires ON oauth_mcp_authorization_codes(expires_at);

CREATE TABLE IF NOT EXISTS oauth_mcp_tokens (
    jti                  TEXT PRIMARY KEY,
    refresh_token_hash   TEXT NOT NULL,
    client_id            TEXT NOT NULL REFERENCES oauth_mcp_clients(client_id) ON DELETE CASCADE,
    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_oauth_mcp_tokens_client ON oauth_mcp_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_mcp_tokens_hash ON oauth_mcp_tokens(refresh_token_hash);

CREATE TABLE IF NOT EXISTS oauth_mcp_signing_keys (
    key_id        TEXT PRIMARY KEY,
    signing_key   TEXT NOT NULL,
    created       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rotated_at    TIMESTAMPTZ
);
