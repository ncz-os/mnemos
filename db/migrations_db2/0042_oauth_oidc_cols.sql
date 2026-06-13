-- 0042_oauth_oidc_cols.sql — OIDC columns on Db2 oauth_identities / oauth_sessions
-- (cross-backend parity; mirrors oracle 0044). The OAuth identity/session methods
-- are written against the OIDC schema (oauth_identities.provider/email/
-- display_name/raw_claims; oauth_sessions.session_id/user_id/revoked) but the Db2
-- migrations created the token-hash shape, so those columns never existed. Added
-- additively (token-hash columns retained). Does NOT touch oauth_providers — the
-- providers OIDC migration requires storing the client_secret in plaintext
-- (get_provider/build_client), which is a separate operator security decision.

ALTER TABLE oauth_identities ADD COLUMN provider VARCHAR(100);
ALTER TABLE oauth_identities ADD COLUMN email VARCHAR(255);
ALTER TABLE oauth_identities ADD COLUMN display_name VARCHAR(255);
ALTER TABLE oauth_identities ADD COLUMN raw_claims CLOB(1M);

CALL SYSPROC.ADMIN_CMD('REORG TABLE oauth_identities');

ALTER TABLE oauth_sessions ADD COLUMN session_id VARCHAR(255);
ALTER TABLE oauth_sessions ADD COLUMN user_id VARCHAR(100);
ALTER TABLE oauth_sessions ADD COLUMN user_agent VARCHAR(500);
ALTER TABLE oauth_sessions ADD COLUMN ip_address VARCHAR(45);
ALTER TABLE oauth_sessions ADD COLUMN revoked SMALLINT NOT NULL DEFAULT 0;

CALL SYSPROC.ADMIN_CMD('REORG TABLE oauth_sessions');

CREATE UNIQUE INDEX uq_oauth_sessions_session_id ON oauth_sessions (session_id) EXCLUDE NULL KEYS;

CREATE INDEX idx_oauth_identities_provider_ext ON oauth_identities (provider, external_id);
