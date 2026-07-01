-- ---------------------------------------------------------------------------
-- MNEMOS v3.0.0 migration: OAuth/OIDC authentication
--
-- Adds browser-based login via Google, GitHub, Azure AD, and generic OIDC
-- providers (Keycloak, Authentik, Auth0, Okta). Coexists with API-key auth:
-- Bearer tokens still work; session cookies are an additional auth path.
--
-- Sessions are DB-backed (revocable, trackable) rather than JWT-encoded.
-- ---------------------------------------------------------------------------

-- Provider registry. Admin configures each provider via /admin/oauth/providers.
CREATE TABLE IF NOT EXISTS oauth_providers (
    name            TEXT         PRIMARY KEY,                         -- 'google' | 'github' | 'azure' | custom
    display_name    TEXT         NOT NULL,
    kind            TEXT         NOT NULL DEFAULT 'oidc',              -- 'oidc' | 'oauth2'
    issuer_url      TEXT,                                              -- required for OIDC discovery
    client_id       TEXT         NOT NULL,
    client_secret   TEXT         NOT NULL,                             -- stored plaintext in DB; mount with tight permissions
    scope           TEXT         NOT NULL DEFAULT 'openid profile email',
    authorize_url   TEXT,                                              -- oauth2-only override
    token_url       TEXT,                                              -- oauth2-only override
    userinfo_url    TEXT,                                              -- oauth2-only override
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    created         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT oauth_provider_kind CHECK (kind IN ('oidc', 'oauth2')),
    CONSTRAINT oauth_oidc_needs_issuer CHECK (
        (kind = 'oauth2') OR (kind = 'oidc' AND issuer_url IS NOT NULL)
    ),
    CONSTRAINT oauth_oauth2_needs_urls CHECK (
        (kind = 'oidc') OR (kind = 'oauth2' AND authorize_url IS NOT NULL AND token_url IS NOT NULL)
    )
);

-- Identity: a (provider, external_id) pair linked to a MNEMOS user.
-- Same MNEMOS user can have multiple identities (e.g. Google + GitHub).
CREATE TABLE IF NOT EXISTS oauth_identities (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT         NOT NULL REFERENCES oauth_providers(name) ON DELETE CASCADE,
    external_id     TEXT         NOT NULL,                             -- OIDC 'sub' claim or provider user ID
    email           TEXT,
    display_name    TEXT,
    raw_claims      JSONB,
    last_login_at   TIMESTAMPTZ,
    created         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE(provider, external_id)
);

CREATE INDEX IF NOT EXISTS idx_oauth_identities_user ON oauth_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_identities_email ON oauth_identities(email) WHERE email IS NOT NULL;

-- DB-backed session store. Cookie value is session_id.
-- Revocable per row; expired rows garbage-collected by a periodic worker.
CREATE TABLE IF NOT EXISTS oauth_sessions (
    session_id      TEXT         PRIMARY KEY,                          -- 64+ char random string, set as cookie
    user_id         TEXT         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    identity_id     UUID         REFERENCES oauth_identities(id) ON DELETE SET NULL,
    created         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ  NOT NULL,
    last_used_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    revoked         BOOLEAN      NOT NULL DEFAULT FALSE,
    user_agent      TEXT,                                              -- captured at create
    ip_address      INET,                                              -- captured at create
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_oauth_sessions_user ON oauth_sessions(user_id) WHERE NOT revoked;
CREATE INDEX IF NOT EXISTS idx_oauth_sessions_expires ON oauth_sessions(expires_at) WHERE NOT revoked;
