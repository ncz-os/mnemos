-- migration: v7_0_default_user_seed_sqlite
-- target:    SQLite 3.40+
-- purpose:   seed the 'default' root user, matching the seed Postgres has
--            carried since migrations_v1_multiuser.sql:
--                INSERT INTO users (id, display_name, role)
--                VALUES ('default', 'Default User', 'root')
--                ON CONFLICT (id) DO NOTHING;
--            Without it the edge profile has an empty users table, and
--            api_keys.user_id has nothing to join against -- so the
--            installer-minted key and every admin-minted key would
--            authenticate against a row that does not exist
--            (lookup_api_key INNER JOINs users). Seeding the row does
--            not grant access on its own; a key must still be minted.
--            SQLite's users table requires a non-null UNIQUE username
--            and has no display_name column, so the id doubles as the
--            username.

INSERT OR IGNORE INTO users (id, username, role, namespace)
VALUES ('default', 'default', 'root', 'default');
