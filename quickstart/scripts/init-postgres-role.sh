#!/bin/bash
# Runs once, on first Postgres init (docker-entrypoint-initdb.d), BEFORE mnemos connects.
#
# mnemos self-provisions its own schema on first connect, but its multiuser / row-level-
# security migration GRANTs privileges to a lower-privilege application role `mnemos_user`
# that it assumes already exists. A stock Postgres container only creates POSTGRES_USER, so
# without this the migration aborts at `GRANT ... TO mnemos_user` and the app never starts.
# Creating the role up front lets the whole migration set apply cleanly in a single pass.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	DO \$\$ BEGIN
	  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mnemos_user') THEN
	    CREATE ROLE mnemos_user LOGIN PASSWORD '${POSTGRES_PASSWORD}';
	  END IF;
	END \$\$;
SQL
echo "[init] ensured role mnemos_user exists"
