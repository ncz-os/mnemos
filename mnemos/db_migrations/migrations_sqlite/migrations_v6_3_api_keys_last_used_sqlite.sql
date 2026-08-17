-- migration: v6_3_api_keys_last_used_sqlite
-- target:    SQLite 3.40+
-- purpose:   add `last_used` column to api_keys so the backend-neutral
--           OAuthRepository.touch_api_key can record the last call time
--           on every backend. Parity with Postgres migrations_v1_multiuser.sql
--           (api_keys.last_used) and Oracle migrations_oracle/0003_api_keys.sql
--           (last_used_at — Oracle's path uses SYSTIMESTAMP into last_used_at).

ALTER TABLE api_keys ADD COLUMN last_used TEXT;
