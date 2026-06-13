-- 0044_oauth_oidc_cols.sql — OIDC columns on oauth_identities / oauth_sessions
-- (cross-backend parity + correctness). The OAuth repository code (the IMPL
-- methods get_identity_for_session / revoke_session AND the provision/create
-- methods) is written against the OIDC schema — it references
-- oauth_identities.provider / email / display_name / raw_claims and
-- oauth_sessions.session_id / user_id / revoked — but the oracle migrations
-- created a token-hash shape (provider_id / profile; access_token_hash /
-- refresh_token_hash / revoked_at). Those columns never existed, so the OAuth
-- methods could not run. Add the OIDC columns additively (the token-hash columns
-- remain, unused). This does NOT touch oauth_providers (the client_secret
-- storage decision is separate).

ALTER TABLE oauth_identities ADD (
  provider     VARCHAR2(100),
  email        VARCHAR2(255),
  display_name VARCHAR2(255),
  raw_claims   CLOB
);

ALTER TABLE oauth_sessions ADD (
  session_id VARCHAR2(255),
  user_id    VARCHAR2(100),
  user_agent VARCHAR2(500),
  ip_address VARCHAR2(45),
  revoked    NUMBER(1) DEFAULT 0 NOT NULL
);

CREATE UNIQUE INDEX uq_oauth_sessions_session_id ON oauth_sessions (session_id);

CREATE INDEX idx_oauth_identities_provider_ext ON oauth_identities (provider, external_id);
