-- migration: 0065_api_keys_key_prefix
-- purpose:   api_keys.key_prefix parity across every backend so the
--            backend-neutral OAuthRepository.create_api_key returns the
--            same Row shape everywhere. ApiKeyResponse.key_prefix is a
--            required str, so a backend without the column could not
--            serve POST /admin/users/{user_id}/apikeys at all.
--            Additive and nullable: existing rows keep NULL, which only
--            affects the cosmetic listing prefix, never authentication
--            (that resolves on key_hash).
-- target:    MySQL 9.x / MariaDB 12.x
-- note:      0052_oauth_repository.sql created api_keys with only the
--            columns the auth lookup needed (id/user_id/key_hash/
--            revoked/last_used). The admin API-key surface also needs
--            key_prefix, label and created_at. Applied by
--            mnemos/persistence/mysql.py::_ensure_mysql_oauth_schema,
--            which tolerates MySQL error 1060 (duplicate column) so
--            replay on an already-migrated database is a no-op.

ALTER TABLE api_keys ADD COLUMN key_prefix VARCHAR(16);
ALTER TABLE api_keys ADD COLUMN label VARCHAR(255);
ALTER TABLE api_keys ADD COLUMN created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
