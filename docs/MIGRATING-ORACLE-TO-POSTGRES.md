# Migrating a MNEMOS deployment from Oracle to PostgreSQL

Applies to MNEMOS 7.1.1. PostgreSQL (16, with pgvector) is MNEMOS's first-class open-source backend; this is the
procedure for moving an existing Oracle-backed deployment onto it with no data loss, and the problems a real
migration ran into. The Hive Mind bus tables that historically shared the Oracle schema (`HIVE_*`) can be moved in the
same run into their own database.

Everything here was rehearsed end to end on a production-sized copy (96 Oracle tables, 6.2 M rows, 43 k memories)
before the real cutover; a full copy takes about three minutes (~50 k rows/s).

## Tools

| file | purpose |
|---|---|
| `scripts/oracle_to_postgres_copy.py` | table-by-table copy with type conversion and row-count parity |
| `scripts/postgres_reassign_ownership.sql` | hand every schema object to the application role |
| `scripts/backfill_embeddings_http.py` | regenerate embeddings from an OpenAI-compatible `/v1/embeddings` endpoint (now also for `postgresql://` DSNs) |

## 1. Build the target schema

The installer migration list (`mnemos/installer/db.py::run_migrations`, applied by
`scripts/ci_apply_postgres_migrations.py`) creates only part of the schema: about 55 relations. MNEMOS creates the
rest at startup. Use the schema of a running 7.1.1 PostgreSQL deployment instead, so every table the copy needs
exists:

```bash
pg_dump --schema-only --no-owner --no-privileges -d mnemos > mnemos-schema.sql   # from a running 7.1.1 instance
```

On the target:

```sql
CREATE ROLE mnemos_user LOGIN PASSWORD '...';
CREATE ROLE mnemos NOLOGIN;                     -- referenced by GRANTs in the migrations
CREATE DATABASE mnemos OWNER mnemos_user;
\c mnemos
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

Then load `mnemos-schema.sql` and run `scripts/postgres_reassign_ownership.sql` as the superuser.

**MNEMOS must own its tables.** It runs additive `ALTER TABLE` migrations at every startup; if the schema was loaded
by the superuser, startup fails with `must be owner of table memories`. The ownership script reassigns tables,
views, sequences (except serial/identity sequences, which follow their table), routines and types, skipping objects
that belong to an extension.

## 2. Copy the data

```bash
python3 scripts/oracle_to_postgres_copy.py --oracle 'user/password@host:1521/SERVICE' \
    --pg 'postgresql://postgres:...@host:5432/mnemos' --target mnemos --dry-run    # column report, no writes
python3 scripts/oracle_to_postgres_copy.py --oracle ... --pg ... --target mnemos \
    --drop-constraints hive_jobs.ck_hive_jobs_status,hive_agents.ck_hive_agents_status
```

Stop every writer (the MNEMOS API and anything else holding Oracle sessions; check `v$session`) before the final
copy. The copier:

* copies every BASE TABLE of the target whose name exists in Oracle; columns are the intersection by name
  (`--dry-run` lists columns present on only one side; nothing is guessed);
* converts by the **target** column type: `JSON` -> json/jsonb text, `RAW(16)` -> uuid, other `RAW`/`BLOB` -> bytea,
  `TIMESTAMP WITH TIME ZONE` via `SYS_EXTRACT_UTC` (the Python driver otherwise drops the offset), epoch numbers into
  timestamp columns, `NUMBER(1)` -> boolean;
* skips `vector` columns: regenerate embeddings afterwards (step 4) rather than converting Oracle `VECTOR` values;
* empties the whole selection with **one** `TRUNCATE` and no `CASCADE` before loading, loads with
  `session_replication_role = replica`, moves serial sequences past `max(id)`, and recounts **every** table after the
  whole run. Exit status 1 on any error, count mismatch, or table that changed after its own copy.

`--tables` / `--exclude` limit the run; `--target hive` copies the Hive Mind bus tables into a database initialised by
the bus's own postgres backend (`HIVE_*` Oracle names map to the bus's names).

## 3. Problems found in a real migration (and what the copier does)

| problem | effect | handling |
|---|---|---|
| per-table `TRUNCATE ... CASCADE` | reloading one table silently emptied `memories` (43 k rows -> 0) through a foreign key | one up-front `TRUNCATE` without `CASCADE`; a referencing table outside the selection makes postgres refuse instead; final recount of all tables |
| reserved-word columns (`mode`) were created quoted lower case in Oracle | `ORA-00904: "MODE": invalid identifier` | the exact Oracle identifier is used in the SELECT |
| hand-written ids (`model_registry`: e.g. `claude-opus-4-8`) in a column that is `uuid` on postgres | `badly formed hexadecimal UUID string` | deterministic `uuid5(NAMESPACE_URL, "ora2pg:<id>")` |
| `hive_jobs` / `hive_agents` status CHECKs are stricter on postgres than on Oracle | rows with `dead-letter`, `needs-review` rejected | `--drop-constraints` (the Oracle data is authoritative); tightening them is a schema follow-up |
| Oracle kept NULL where postgres says `NOT NULL` without a default (e.g. `graeae_consultations.consensus_response` of failed consultations) | `NotNullViolationError` | NULL -> `''` for NOT NULL **text** columns only; any other type still fails loudly |
| columns renamed between the schemas (`model_registry.raw` vs Oracle `raw_payload`) | data silently not copied | the `RENAMES` map; check `--dry-run` output for columns that exist on one side only |
| comment-only or `;`-in-comment SQL chunks | asyncpg cannot execute them | relevant when applying DDL scripts statement by statement: split outside quotes and drop `--` comments |

Tables that a 7.1.1 PostgreSQL schema does not have are not copied: legacy tables with no code references
(`memory_events`, `memory_messages`, `memory_jobs`, `memory_agents`, `knemon_*` except those in the schema,
`llm_providers`), `*_BAK_*` snapshots, and tables whose Oracle shape predates the postgres one
(`knemon_baselines`, `knemon_tier_assignments`). Archive them with Oracle Data Pump before decommissioning.

## 4. Regenerate embeddings

Point an OpenAI-compatible embedding endpoint serving the same model as the deployment (`MNEMOS_EMBED_HTTP_MODEL`,
1024-d for bge-m3) at the new database:

```bash
MNEMOS_DATABASE_DSN='postgresql://mnemos_user:...@host:5432/mnemos' \
MNEMOS_EMBED_HTTP_URL='http://embed-host:port/v1/embeddings' MNEMOS_EMBED_HTTP_MODEL=bge-m3 \
python3 scripts/backfill_embeddings_http.py
```

Only rows with a NULL embedding are touched, so it resumes after an interruption. About 50-65 rows/s on one GPU.
Semantic search falls back to full-text search for rows that have no embedding yet. Use the same endpoint (model
build and quantisation) for query embeddings as for the backfill.

## 5. Switch MNEMOS

```
MNEMOS_PERSISTENCE_BACKEND=postgres
MNEMOS_DATABASE_DSN=postgresql://mnemos_user:...@127.0.0.1:5432/mnemos
```

Restart, then check `/health` (`persistence_backend: PostgresBackend`, `database_connected: true`), `/stats`, a
lexical and a semantic search, and a write. Keep the Oracle instance stopped, not deleted, until the new deployment
has run cleanly; it is the rollback.

## 6. Replication

PostgreSQL streaming replication replaces Data Guard: `wal_level=replica`, a `REPLICATION` role, a `pg_hba.conf`
`replication` entry per standby, and `pg_basebackup -R -X stream -C -S <slot>` on each standby. Replicas are
read-only; writes go to the primary.
