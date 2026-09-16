-- migration: 0065_api_keys_key_prefix
-- purpose:   api_keys.key_prefix parity across every backend so the
--            backend-neutral OAuthRepository.create_api_key returns the
--            same Row shape everywhere. ApiKeyResponse.key_prefix is a
--            required str, so a backend without the column could not
--            serve POST /admin/users/{user_id}/apikeys at all.
--            Additive and nullable: existing rows keep NULL, which only
--            affects the cosmetic listing prefix, never authentication
--            (that resolves on key_hash).
-- target:    Oracle 23ai
-- note:      replay raises ORA-01430 (column already exists), which
--            mnemos/persistence/schema.py treats as benign.

ALTER TABLE api_keys ADD (key_prefix VARCHAR2(16));
