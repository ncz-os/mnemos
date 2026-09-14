# Oracle Backend — Status and Coverage

**Backend:** Oracle AI Database 26ai (Free and Enterprise), via `python-oracledb`
async thin client.
**Status:** Shipped and on `master`. The Oracle backend is a first-class
`PersistenceBackend` implementation alongside Postgres, SQLite, MySQL/MariaDB
and Db2.
**Originally drafted:** 2026-05-21 during the M7 port. Content below describes
the current implementation, not the state at that date.

Authoritative module: `mnemos/persistence/oracle.py`. Migrations:
`mnemos/db_migrations/migrations_oracle/`. The legacy `mnemos/db/oracle.py`
is retained only for CHARON-import script compatibility.

---

## Selection and wiring

`mnemos/core/lifecycle.py` selects the Oracle backend from the DSN scheme:

```
oracle://user:pass@host:port/service
oracle+oracledb://user:pass@host:port/service
```

It builds an async pool via `mnemos.persistence.oracle.create_oracle_pool` and
instantiates `OracleBackend`. Handlers and the API layer are dialect-agnostic —
they consume the `PersistenceBackend` ABC, so switching to Oracle is a DSN
change and nothing else.

`OracleBackend.capabilities` declares `core`, `oauth`, `sessions`,
`consultations`, `federation`, `audit`, `state` and `acl`.

## Repository surface

`OracleBackend` exposes 17 repository properties, every one backed by a
concrete Oracle implementation. Attribute lookup never raises.

| Repository property | Class | Methods |
|---|---|---|
| `memories` | `OracleMemoryRepository` | 28 |
| `kg_triples` | `OracleKGRepository` | 3 |
| `memory_versions` | `OracleVersionRepository` | 4 |
| `memory_branches` | `OracleBranchRepository` | 4 |
| `compression` | `OracleCompressionRepository` | 5 |
| `compression_queue` | `OracleCompressionQueueRepository` | 7 |
| `morpheus` | `OracleMorpheusRepository` | 17 |
| `webhooks` | `OracleWebhookRepository` | 13 |
| `nats_dispatch_log` | `OracleNatsDispatchLogRepository` | 1 |
| `consultations_audit` | `OracleConsultationAuditRepository` | 11 |
| `oauth` | `OracleOAuthRepository` | 12 |
| `sessions` | `OracleSessionsRepository` | 9 |
| `consultations` | `OracleConsultationsRepository` | 8 |
| `federation` | `OracleFederationRepository` | 23 |
| `state_kv` | `OracleStateRepository` | 5 |
| `audit_chain` | `OracleAuditChainRepository` | 9 |
| `acl` | `OracleAclRepository` | 4 |

Semantic search runs on Oracle AI Database 26ai `VECTOR(*, FLOAT32)` columns with
`VECTOR_DISTANCE(..., COSINE)`; the `(*, dim)` typing lets one column serve
every configured embedding dimension.

### Model registry / consultation audit

`OracleConsultationAuditRepository` is a real implementation, not a
safe-default shim. It reads and writes the Oracle `model_registry` and
`model_registry_sync_log` tables directly:

- `fetch_available_models` / `_registry_rows` — `SELECT` over `model_registry`
  filtered on `available = 1 AND NVL(deprecated, 0) = 0`, preferring the
  authoritative `provider` column and falling back to model-id/family
  derivation only for legacy rows with a NULL provider.
- `lookup_provider_for_model`, `fetch_model_provider` — provider resolution by
  `model_id`.
- `upsert_model`, `mark_models_unavailable`, `update_arena_score`,
  `upsert_model_pricing`, `write_price_history`, `write_model_sync_log` —
  the full registry-sync write path.

The usage ledger reads `price_in` / `price_out` / `price_cached` from
`model_registry` to compute `est_cost_usd`; reasoning tokens fall back to the
output rate because the Oracle `model_registry` carries no reasoning-cost
column. A missing registry row or missing price is logged and recorded as
`est_cost_usd=0` rather than failing the call.

## VisibilityFilter rendering

`_render_visibility(filter, *, table_alias, param_prefix)` in
`mnemos/persistence/oracle.py` renders a `VisibilityFilter` into an Oracle
`WHERE` clause with named binds. It is a complete port of the multi-user
policy used by Postgres and SQLite — not a partial one.

- `ROOT_BYPASS` → no tenancy clause, or `namespace = :ns` when pinned.
- `OWN_ONLY` → `owner_id = :owner AND namespace = :ns`.
- `READABLE` → the full predicate, namespace-pinned:
  - `owner_id = :owner` (own rows), **OR**
  - `federation_source IS NOT NULL` (federated rows), **OR**
  - `MOD(NVL(permission_mode, 0), 10) >= 4` (world read bit), **OR**
  - `MOD(TRUNC(NVL(permission_mode, 0) / 10), 10) >= 4 AND group_id IS NOT NULL
    AND group_id IN (...)` (group read bit **plus** caller group membership —
    the unix-bits group expansion), **OR**
  - `EXISTS (SELECT 1 FROM memory_acl macl WHERE macl.memory_id = id AND
    macl.principal IN (...) AND BITAND(macl.perm, ACL_READ_BIT) > 0)`
    (explicit ACL grant, over `acl_principals(user_id, group_ids)`).
- A `None` namespace on any non-root scope renders `1=0`, matching Postgres.

On top of the tenancy predicate, `_render_visibility` ANDs a vault-subtraction
term for any namespace in `visibility.exclude_namespaces`:

```sql
(namespace IS NULL OR namespace NOT IN (:vis_xns_0, ...))
```

This is deliberately applied **even under `ROOT_BYPASS`**, which otherwise
emits no tenancy filter and would expose vault rows to a root token. The
`namespace IS NULL` disjunct is load-bearing: `NOT IN` evaluates to UNKNOWN for
NULL namespaces, which would silently drop legitimate non-vault rows. Vault
rows always carry a non-NULL namespace, so NULL is never a secret.

`_render_visibility_core` provides the tenancy-only render without the vault
subtraction, for callers that compose their own outer predicate.

## Migrations and replay safety

Oracle migrations live in `mnemos/db_migrations/migrations_oracle/` as a
numbered chain (`0001_core_schema.sql` through the current head).

**Replay safety is a chain-wide property, not a property of one file.** The
migration runner has no applied-state tracking and replays the full chain on
every start, so every statement must be safe against both a fresh and an
already-migrated database. See
[`docs/PERSISTENCE_ABC_STANDARDIZATION.md`](PERSISTENCE_ABC_STANDARDIZATION.md)
"Item 5" for the standing principle and for how replay is verified against
live Oracle and Db2 instances.

For `0001_core_schema.sql` specifically: Oracle 23c+ supports
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` but **not**
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so every multi-column `ALTER` is
wrapped in a PL/SQL block that checks `user_tab_columns` before issuing the
DDL, and backfill `UPDATE`s are guarded by an `EXISTS` check on the legacy
column. Do not generalise that specific verification to the rest of the chain —
each migration carries its own guards.

`scripts/oracle_apply_migration.py` splits a migration file on sqlplus `/` and
`;` terminators and runs each statement through `oracledb` async, treating
ORA-00955 / ORA-02275 / ORA-01430 / ORA-04081 as idempotent-replay signals.

## Schema notes

`0001_core_schema.sql` creates the core tables plus the sidecars:
`memory_branches`, `state`, `federation_peers`, `federation_sync_log`,
`federation_consolidation_tombstones`, `webhook_subscriptions`,
`webhook_deliveries`, `memory_compression_candidates` and
`memory_compressed_variants`. On `memories` it adds `federation_source`,
`federation_remote_updated`, `recall_count`, `last_recalled_at`,
`content_hash` and `embedding`.

Two deliberate divergences from the Postgres schema:

- **`peer_mnemos_version` / `last_schema_check_at`** on `federation_peers` are
  present and written by `update_peer_schema_check`.
- **`memories.consolidated_into` / `consolidated_at`** — the Oracle path
  records canonicalisation in `federation_consolidation_tombstones` and
  soft-deletes the source row instead of carrying in-place consolidation
  metadata on `memories`.

## Testing

- `tests/test_persistence_interface.py` — ABC contract conformance.
- `tests/test_persistence_parity.py` — cross-backend parity; the Oracle arm
  activates when `ORACLE_DSN` is set.
- `tests/test_oracle_live.py` — live probe against a running instance.
- `tests/test_oracle_recency_dialect.py`,
  `tests/test_oracle_vector_validation.py` — dialect and vector-bind pinning.
- `scripts/oracle_proof_run.py` — runnable repository-surface proof harness,
  emits a signed JSON artifact from a live instance.

Known live-test constraint: Oracle write-path verification requires the test
container's CDB to be open READ WRITE. A CDB opened READ ONLY fails with
`ORA-65054`; see `docs/PERSISTENCE_ABC_STANDARDIZATION.md` "Open items".

## Operational follow-ups

These are genuine open items, not blockers on the backend itself:

- **Oracle Text FTS.** `fts_search` uses a `DBMS_LOB.INSTR` substring locator
  as its deterministic fallback. An inverted index
  (`CREATE INDEX ... INDEXTYPE IS CTXSYS.CONTEXT`) plus tokenizer setup and
  index maintenance is the production answer for large corpora.
- **Vector index tuning.** Linear scan is sub-millisecond at small row counts.
  `CREATE VECTOR INDEX` (IVF on Free, HNSW on Enterprise with
  `vector_memory_size` allocated) is worth adding at scale.
- **Planner statistics.** Run `DBMS_STATS.GATHER_TABLE_STATS` after a bulk
  load; range-scan plans are otherwise chosen on stale cardinality estimates.
- **CI breadth.** The `test:oracle-smoke` job could run a wider pytest subset
  and exercise federation sync paths.

## Performance harness environment

`scripts/oracle_vs_pythia_perf.py` reads all credentials from environment
variables and fails before making any network call if one is missing. The
required set is the `REQUIRED_ENV` tuple at the top of the script:

- `MNEMOS_TOKEN` — bearer token for the source MNEMOS API.
- `ORACLE_PASS` — Oracle password for the `mnemos` database user.
- an SSH-password variable, consumed by `sshpass -e` through `SSHPASS`.

Two optional variables override the SSH user (default `root`) and the target
host. The SSH variable names are prefixed with the deployment's Oracle host
name, so the exact literals are not reproduced here — read them from
`REQUIRED_ENV` and the `os.environ.get` calls at the top of the script.

The harness uses `ssh -o StrictHostKeyChecking=yes`, so the invoking
workstation must already trust the target host key.

**Security note (unresolved):** the harness defaults to SSH as `root` for a
read-only benchmark, which carries host-level blast radius. Create a dedicated
low-privilege benchmark account with a forced command limited to the Oracle
query script before any operational use.
