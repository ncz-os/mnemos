# OAuth backend-agnostic persistence fix report

Date: 2026-09-09 (America/New_York)
Repository: `/tmp/mnemos-audit-clones/mnemos-oauth-backend-fix`
Branch: `feat/oauth-backend-agnostic-store`
Starting HEAD: `1258bad81fd309facf10d1d0f04e09b009f4f954`

## Outcome and limits

Implemented the backend-neutral remote-MCP OAuth store and startup wiring. The gateway no longer creates a dedicated asyncpg pool. It uses `OAuthPersistence.oauth` on an existing app/lifecycle backend, or opens the standalone backend with the existing `mnemos.core.lifecycle.build_configured_persistence_backend` factory and `MNEMOS_DATABASE_DSN`.

Final combined verification: **563 passed, 221 skipped, 0 failures, 0 errors; 55 warnings; 12.88 seconds**. Ruff reports **zero errors** on all changed Python files; the existing changed Python files also had zero Ruff errors at the starting HEAD. `git diff --check` passes.

Real SQLite persistence and standalone ASGI startup/HTTP OAuth behavior are verified. **Live PostgreSQL, Oracle, MySQL, MariaDB, Db2, and the CLI/TCP/authenticated-SSE flow remain unverified.** Passing unit, mock, dialect, and protocol tests are not live database certification. No production database was contacted or changed. No deployment was performed.

The existing checkout and branch were retained. Changes and this report are committed locally as Jason Perlow <jperlow@gmail.com>; nothing was pushed. No repository was cloned and no branch was switched.

## Exact implementation files and functions

| File | Changes |
| --- | --- |
| `mnemos/mcp/oauth.py` | Replaced `PostgresOAuthStore` with `PersistenceOAuthStore`; all eight store operations delegate to the shared OAuth repository inside `backend.transactional()`. `OAuthService.__init__` now types its store as the persistence adapter or explicit in-memory development store. Updated `get_oauth_service` documentation/error to identify `MNEMOS_DATABASE_DSN`. |
| `mnemos/mcp/http.py` | `_load_token_principals` permits OAuth-only configuration with issuer/passphrase and a backend-generated signing key. `_mcp_http_lifespan` reuses app state, then the lifecycle backend, then the existing configured backend factory. Checks OAuth capability, persists/reloads the generated signing key, aborts failed initialization, drains audit tasks, closes only owned backends, and removes only state it attached. No dedicated asyncpg import/pool/schema path remains. |
| `mnemos/core/config.py` | `_OAuthSettings` documents shared persistence; retains `database_url` solely so startup can reject old configuration with actionable migration instructions. |
| `mnemos/persistence/base.py` | Extended `OAuthRepository` with the eight MCP operations listed below. The existing `OAuthPersistence` protocol continues to expose this repository through `.oauth`; no parallel capability was invented. |
| `mnemos/persistence/mcp_oauth.py` (new) | `MCPOAuthRepositoryMixin` implements shared DCR/code/token/key behavior; `oauth_utc` normalizes timestamps; `_valid_opaque_id` rejects malformed identifiers before collation-sensitive SQL. Native adapters supply bindings, timestamp encoding, affected-row counts, and lock clauses. |
| `mnemos/persistence/postgres.py` | `PostgresOAuthRepository` adds the shared mixin plus `_mcp_sql`, `_mcp_fetch`, `_mcp_execute`; translates placeholders to asyncpg parameters using the existing transaction connection. Existing PostgreSQL OAuth tables and migration are unchanged. |
| `mnemos/persistence/sqlite.py` | `SqliteOAuthRepository` adds the mixin, native fetch/execute and UTC text encoding; `SQLITE_MIGRATION_FILES` includes the new OAuth migration. Uses existing `BEGIN IMMEDIATE` transaction handling. |
| `mnemos/persistence/oracle.py` | `OracleOAuthRepository` adds the mixin, named bindings, cursor operations, UTC timestamp conversion, and duplicate-safe `mcp_save_signing_key`. Existing row materialization handles CLOBs. |
| `mnemos/persistence/db2.py` | `Db2OAuthRepository` supplies native question-mark bindings and `WITH RS USE AND KEEP UPDATE LOCKS`. Both normal and native Db2 facades inherit the complete repository operations. |
| `mnemos/persistence/mysql.py` | Added `MysqlOAuthRepository`, binary-ID decoding/native bindings, UTC encoding, `_ensure_mysql_oauth_schema`, repository property/initialization and OAuth capability advertisement; backend `open` provisions its tables through the existing connection. |
| `mnemos/persistence/mysql_oauth.py` (new) | `MysqlBrowserOAuthMixin` implements the ten pre-existing provider/identity/browser-session/API-key operations that this checkout's MySQL backend lacked. `provision_or_link_user` handles concurrent verified-email identity linking with a locking read of the winning identity. |
| `mnemos/persistence/mariadb.py` | Initializes the shared MySQL-family OAuth repository, advertises the OAuth capability, and invokes the same OAuth schema setup during its existing `open` path. |

### Important premise correction

This starting checkout did **not** implement `OAuthPersistence` for MySQL/MariaDB: it had no OAuth repository property or capability. Completing that existing contract was necessary to make the requested common OAuth capability available on every backend. The additions are confined to OAuth persistence and its prerequisites, not unrelated MySQL subsystems.

## Protocol extension and parity

The eight new abstract repository methods, all taking the caller's transaction, are:

- `mcp_get_signing_key(tx)`
- `mcp_save_signing_key(tx, *, key_id, signing_key)`
- `mcp_save_client(tx, row)`
- `mcp_get_client(tx, client_id)`
- `mcp_save_code(tx, row)`
- `mcp_consume_code(tx, code)`
- `mcp_save_token(tx, row)`
- `mcp_rotate_refresh(tx, token_hash, client_id, successor)`

Each is implemented by `PostgresOAuthRepository`, `SqliteOAuthRepository`, `OracleOAuthRepository`, `MysqlOAuthRepository` (also used by MariaDB), and `Db2OAuthRepository` through the shared mixin/native overrides described above.

**Required method-set parity is verified:** all five backend families, MariaDB, and native Db2 satisfy `OAuthPersistence`, expose a concrete `OAuthRepository`, and implement all **18** required async methods with an empty `__abstractmethods__` set. The contract tests instantiate all seven concrete facades without opening databases. This verifies method availability, not untested external-engine SQL semantics. Classes may retain additional existing backend-specific helper methods.

## Schema and signing-key persistence

Added migrations:

| File | Runtime application |
| --- | --- |
| `mnemos/db_migrations/migrations_sqlite/migrations_v6_3_mcp_oauth_sqlite.sql` | SQLite's existing migration list. |
| `mnemos/db_migrations/migrations_oracle/0053_mcp_oauth.sql` | Existing numbered Oracle schema discovery. |
| `mnemos/db_migrations/migrations_db2/0053_mcp_oauth.sql` | Existing numbered Db2 schema discovery; native Db2 DDL with explicit non-null primary keys. |
| `mnemos/db_migrations/migrations_mysql/0052_oauth_repository.sql` | MySQL/MariaDB `open`: prerequisites for the previously missing common OAuth repository. |
| `mnemos/db_migrations/migrations_mysql/0053_mcp_oauth.sql` | MySQL/MariaDB `open`: MCP clients, codes, refresh families, signing keys. |

PostgreSQL retains its existing `oauth_mcp_clients`, `oauth_mcp_authorization_codes`, `oauth_mcp_tokens`, and `oauth_mcp_signing_keys` tables and data. The other backends provision the same logical tables. Oracle/Db2 redirect fields use CLOBs to avoid byte-length rejection of valid Unicode redirect URIs. MySQL/MariaDB opaque identifiers use `VARBINARY` to avoid case/padding aliases; the adapter decodes returned bytes consistently.

Without an explicit signing-key override, startup reads the stored default key. On first boot it generates `secrets.token_urlsafe(32)`, inserts only if absent, then reads back the winning value. PostgreSQL/SQLite use conflict-do-nothing; MySQL-family uses a no-op duplicate update; Oracle/Db2 suppress only recognized uniqueness violations. The caller transaction commits before the winning key is re-read. This avoids concurrent first boots installing different keys. An explicit `MNEMOS_OAUTH_SIGNING_KEY` still overrides the stored value.

Authorization-code use is a conditional update and read in one transaction. Refresh rotations/replays lock the immutable family root and then read current state under a lock, covering MySQL repeatable-read snapshots and stale-ancestor replay. Successor insertion and parent revocation commit or roll back together. JWT access validation remains stateless as before; this change preserves existing refresh-family revocation semantics rather than introducing a new access-token revocation policy.

## Configuration and documentation

Updated `.env.example`, `AGENTS.md` connectivity instructions, `docs/connectors/chatgpt-pro-developer-mode.md`, and `docs/connectors/codex-cli.md`.

`MNEMOS_OAUTH_DATABASE_URL` is removed as an operational setting. If nonempty, MCP startup fails clearly, instructing the operator to unset it and configure `MNEMOS_DATABASE_DSN`. There is no silent fallback to another database. Pointing the shared DSN at the original PostgreSQL database preserves its OAuth tables. Operators moving an existing separate OAuth database must migrate clients/grants/keys to preserve authorizations; this patch does not copy production data.

No changes were needed in `mnemos/core/lifecycle.py`: the exact standalone factory already existed and is reused.

## Test changes and measured results

Updated all three requested files rather than dropping PostgreSQL coverage:

- `tests/test_mcp_oauth.py`: backend-parametrized exact redirect/client-store behavior; retained in-memory crypto and protocol unit tests.
- `tests/test_mcp_oauth_integration.py`: real repository fixture across all backend options; app-backend reuse/ownership and old-variable failure; retained real CLI/MCP SDK SSE test, now configured with SQLite DSN and no explicit signing key or old OAuth URL.
- `tests/test_mcp_oauth_live_restart.py`: backend-open provisioning, persisted signing/client/code/token state, backend reopen, concurrent code consumption, refresh replay/family invalidation, transaction rollback on insertion failure, expiry and exact client/code/hash matching. Preserved the PostgreSQL deterministic stale-ancestor trigger-race test.
- `tests/oauth_backend_helpers.py` (new): SQLite by default; disposable explicit DSNs for PostgreSQL/Oracle/MySQL/MariaDB/Db2. PostgreSQL uses an isolated schema. An explicitly configured backend that cannot open fails rather than skips.
- `tests/test_mcp_oauth_backend_lifecycle.py` (new): lifecycle-owned backend reuse, cleanup after startup failure, and a fresh-interpreter standalone SQLite ASGI startup/HTTP/reopen test.
- `tests/persistence/test_mcp_oauth_contract.py` (new): required method parity on seven concrete facades.
- `tests/persistence/test_mcp_oauth_validation.py` (new): malformed opaque identifiers rejected before database comparisons across all five repository implementations.
- `tests/persistence/test_mysql_oauth_repository.py` (new): concurrent identity-insert conflict recovery and unrelated-error propagation.
- `tests/test_mysql_backend.py`: updates the capability assertion to include the newly implemented OAuth capability.

### OAuth matrix from final run

| Backend / group | Passed | Failed/errors | Skipped | Evidence meaning |
| --- | ---: | ---: | ---: | --- |
| SQLite parameter cases | 40 | 0 | 1 | Real SQLite persistence; skip is the PostgreSQL-only trigger test. |
| PostgreSQL parameter cases | 0 | 0 | 41 | Disposable test DSN absent; live OAuth unverified. |
| Oracle parameter cases | 0 | 0 | 41 | Disposable test DSN absent; live OAuth unverified. |
| MySQL parameter cases | 0 | 0 | 41 | Disposable test DSN absent; live OAuth unverified. |
| MariaDB parameter cases | 0 | 0 | 41 | Disposable test DSN absent; live OAuth unverified. |
| Db2 parameter cases | 0 | 0 | 41 | Disposable test DSN absent; live OAuth unverified. |
| Shared crypto/protocol plus standalone ASGI case | 25 | 0 | 1 | Includes one fresh-interpreter SQLite startup/HTTP/reopen pass; real network/SSE case skipped. |

### Additional backend and shared regression suites

These numbers are from the same final run and are disjoint from the OAuth matrix above. Server-engine passes here are unit/mock/dialect checks, not live DB passes.

| Suite group | Passed | Failed/errors | Skipped |
| --- | ---: | ---: | ---: |
| SQLite-prefixed regression files | 38 | 0 | 2 |
| PostgreSQL-prefixed regression files | 140 | 0 | 0 |
| Oracle-prefixed regression files | 30 | 0 | 1 module skip (missing driver) |
| Db2-prefixed regression files | 129 | 0 | 2 (missing-driver module and unavailable lifecycle hook) |
| MySQL-prefixed regression files | 15 | 0 | 8 live tests |
| MariaDB-prefixed regression files | 7 | 0 | 1 live test |
| Shared persistence/auth/lifecycle and new contract/validation/race tests | 139 | 0 | 0 |
| **Entire final run** | **563** | **0** | **221** |

SQLite's two additional skips require the optional `mnemos_hot` native extension. Fifty-five warnings were emitted; they are reported as warnings, not counted as passing checks.

### SQLite-only functional result

**PASS, with precisely bounded scope:** a fresh Python interpreter clears MNEMOS/PG backend configuration, uses only `MNEMOS_DATABASE_DSN=sqlite:///...`, issuer and admin passphrase, and imports the real standalone MCP app. With no old URL, no signing-key override and no static bearer, it opens the actual lifespan, generates/persists the key, serves discovery/DCR/PKCE authorization/token exchange over HTTPX ASGI transport, validates the JWT, closes/reopens the backend, and successfully refreshes with the same persisted signing key.

**NOT RUN successfully:** `mnemos serve mcp-http` over TCP followed by real authenticated SDK SSE initialization/list-tools. The retained test is skipped because loopback `socket.bind` raises `PermissionError(1, 'Operation not permitted')`. The ASGI result does not prove CLI startup, TCP, SSE, or restart between two separate server processes.

## Review and remaining blockers

Zoder was attempted first for authoring: its local engine did not start and provider fallbacks failed to connect. The final zoder review attempt also failed: **0/1 reviewers completed**, with an NVIDIA endpoint connection error. Its failure output is not an approval.

Secondary independent Codex review approved the gateway and final persistence changes after fixes for concurrent MySQL identity linking and padding-sensitive credential aliases. The reviewer independently executed contract/race/restart and malformed-input tests, and explicitly limited approval to static/unit/SQLite evidence. No external-engine runtime certification is claimed.

Remaining blockers:

1. No Docker daemon: `/var/run/docker.sock` does not exist.
2. Package network access fails at DNS resolution for PyPI. Python 3.13 cached packages enabled local testing; `oracledb`, `aiomysql`, and `ibm_db` are unavailable. No heavy builds were run.
3. No disposable external test DSNs were configured. Oracle/MySQL/MariaDB/Db2/PostgreSQL live OAuth schema application, driver binding, locking, concurrent first boot, refresh races and restart survival remain unverified. Native Db2 is covered by concrete protocol checks/static native SQL review, not a live instance.
4. Loopback binding is denied, blocking the CLI/TCP/SSE functional test.
5. MNEMOS search/store and Hive registration/submission were attempted but rejected by the tool layer: approval is required while session approval policy is `never`. No cross-session MNEMOS record or Hive job was successfully written.

An early expanded test collection hit a missing optional `zstandard` dependency. Installing its cached wheel allowed the affected suites to run in the final successful command. An old MySQL capability assertion failed during development and was updated for the new OAuth capability; it passes in the final run.

## Reproduction and evidence

Final command (Python 3.13 local `.venv`, cached dependencies):

```sh
.venv/bin/python -m pytest tests/persistence tests/test_auth_persistence_neutral.py tests/test_db2_dialect_parity.py tests/test_db2_live.py tests/test_db2_migration_syntax.py tests/test_db2_native_cursor.py tests/test_db2_semantic_search_dialect.py tests/test_db2_session_ownership.py tests/test_db2_translation_string_safety.py tests/test_mariadb_backend.py tests/test_mcp_oauth.py tests/test_mcp_oauth_backend_lifecycle.py tests/test_mcp_oauth_integration.py tests/test_mcp_oauth_live_restart.py tests/test_mysql_backend.py tests/test_mysql_branches_live.py tests/test_mysql_compression_live.py tests/test_mysql_consaudit_live.py tests/test_mysql_family_json_binding.py tests/test_mysql_federation_live.py tests/test_mysql_kg_live.py tests/test_mysql_lifecycle_schema.py tests/test_mysql_recency_dialect.py tests/test_mysql_state_live.py tests/test_mysql_upsert_semantics.py tests/test_mysql_versions_live.py tests/test_oracle_live.py tests/test_oracle_model_registry.py tests/test_oracle_nullability_noop_is_benign.py tests/test_oracle_persistence_data_health.py tests/test_oracle_recency_dialect.py tests/test_oracle_vector_validation.py tests/test_persistence_conformance.py tests/test_persistence_interface.py tests/test_persistence_parity.py tests/test_postgres_embedding_dim.py tests/test_postgres_only_503_invariant.py tests/test_postgres_semantic_search_dim_validation.py tests/test_sqlite_cosine_rust_optin.py tests/test_sqlite_embedding_dim.py tests/test_sqlite_insert_dup.py tests/test_sqlite_recency.py tests/test_sqlite_upgrade_idempotency.py tests/test_worker_lifecycle_backends.py -q -ra --disable-warnings --junitxml=/tmp/mnemos-oauth-validation/final-regression.xml
```

Evidence retained locally:

- `/tmp/mnemos-oauth-validation/final-regression.txt`
- `/tmp/mnemos-oauth-validation/final-regression.xml`
- `/tmp/mnemos-oauth-validation/final-ruff.txt`
- `/tmp/mnemos-oauth-validation/ruff-head-baseline.json`
- `/tmp/mnemos-oauth-validation/standalone-sqlite.txt`
- `/tmp/mnemos-oauth-validation/zoder-review.txt`

To enable external OAuth tests, supply disposable test databases using `MNEMOS_TEST_OAUTH_POSTGRES_DSN`, `MNEMOS_TEST_OAUTH_ORACLE_DSN`, `MNEMOS_TEST_OAUTH_MYSQL_DSN`, `MNEMOS_TEST_OAUTH_MARIADB_DSN`, and `MNEMOS_TEST_OAUTH_DB2_DSN`, with their drivers installed. PostgreSQL tests also require a database able to run normal backend schema provisioning. Do not point these fixtures at production: external non-PostgreSQL fixtures provision schema and retain test rows.
