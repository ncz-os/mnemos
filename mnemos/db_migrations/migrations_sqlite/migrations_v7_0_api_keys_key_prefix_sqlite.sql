-- migration: v7_0_api_keys_key_prefix_sqlite
-- target:    SQLite 3.40+
-- purpose:   add `key_prefix` to api_keys so the backend-neutral
--            OAuthRepository.create_api_key returns the same Row as
--            Postgres. ApiKeyResponse.key_prefix is a required str, so
--            without this column the edge profile could not serve
--            POST /admin/users/{user_id}/apikeys.
--            Mirrors migrations_v6_3_api_keys_last_used_sqlite.sql,
--            which added `last_used` for the same parity reason.
--            Replay is a no-op: _apply_migrations swallows SQLite's
--            "duplicate column name".

ALTER TABLE api_keys ADD COLUMN key_prefix TEXT;
