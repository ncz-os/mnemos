# MNEMOS OAuth/Morpheus Audit Fix Report

Date: 2026-09-09

Branch: `feat/oauth-mcp-provider-agnostic-v2`

Implementation commit: `7628ef0` (`fix(mcp): close OAuth audit findings`)

## Outcome

All eight requested findings were fixed in one implementation pass and covered by regression tests. The repository's exact `make test` target completed successfully with **3,296 passed, 97 skipped, 0 failed**. The real OAuth-to-MCP SSE `tools/list` test ran and passed. A live fresh-PostgreSQL run could not be completed in this sandbox; the exact limitation is recorded under A1 and Test Results.

## S1 - Shared bounded authorization-attempt throttling

- **Before:** `mnemos/mcp/oauth.py` performed the admin-passphrase comparison before client lookup without throttling, and the token endpoint was likewise unthrottled.
- **After:** `mnemos/mcp/oauth.py:43-47,80-167` defines a bounded, monotonic sliding-window limiter with capped caller bookkeeping, exponential backoff, and `Retry-After`. `mnemos/mcp/oauth.py:438-458` creates one shared authorization bucket. `mnemos/mcp/oauth.py:587-614` checks it before the constant-time admin-passphrase comparison, and `mnemos/mcp/oauth.py:654-668,896-915` checks it before token client lookup/authentication. Caller identity is keyed by request IP, with a client-ID fallback when no peer IP exists. `hmac.compare_digest` remains in both credential checks.
- **Regression tests:** Added `tests/test_mcp_oauth_integration.py:1393-1433`, proving ten attempts are accepted, the next returns 429 with `Retry-After`, authorize and token share the bucket, and token throttling occurs before client lookup.

## S2 - Fail-closed HS256 signing-key validation

- **Before:** `mnemos/mcp/oauth.py` rejected only falsey signing keys, so one-character and whitespace-only secrets could reach PyJWT.
- **After:** `mnemos/mcp/oauth.py:414-422` rejects non-strings, empty/whitespace-padded values, keys shorter than 32 UTF-8 bytes, known placeholder values, and repeated-character placeholders at `OAuthService` construction time. A valid 32-byte non-placeholder key remains accepted.
- **Regression tests:** Added/expanded `tests/test_mcp_oauth_integration.py:1353-1390` for short, whitespace, placeholder, repeated-character, and exact-32-byte cases. Existing fixtures in `tests/test_mcp_oauth.py` were strengthened to use valid secrets.

## S5 - Vault exclusion at Morpheus eligibility boundary

- **Before:** `eligible_for_morpheus()` returned only the canonical deleted/archived/consolidated predicate, allowing `namespace='vault'` rows into LLM extraction candidates.
- **After:** `mnemos/core/eligibility.py:23-27` applies exactly the same null-safe vault exclusion pattern used by compression: `(namespace IS NULL OR namespace <> 'vault')`.
- **Regression test:** Added the executable SQLite predicate test `tests/test_eligibility_predicate.py:84-100`; it proves an otherwise eligible vault row is absent while ordinary and null-namespace rows remain eligible. Morpheus extraction fakes also enforce the predicate.

## A1 - Runtime registration of OAuth PostgreSQL migration

- **Before:** `migrations_v5_4_0_mcp_oauth.sql` was not present in `_POSTGRES_LEGACY_MIGRATIONS`, and it is outside the numbered migration directory, so `PostgresBackend.open()` did not apply it.
- **After:** `mnemos/persistence/schema.py:76-85` registers the OAuth migration in the legacy runtime sequence.
- **Regression tests:** `tests/test_schema_standup.py:84-124` actually calls `PostgresBackend.open()` through a fresh-schema asyncpg lifecycle double and asserts all four OAuth `CREATE TABLE` statements are executed. `tests/test_mcp_oauth_live_restart.py:200-242` creates a unique schema, opens a real `PostgresBackend`, and queries `information_schema` for all four OAuth tables when a live DSN is available.
- **Live-DB limitation:** No DSN was provided, so the live test correctly skipped. Two attempts to start an isolated local PostgreSQL 17 cluster under `/tmp` failed during `initdb` with `FATAL: could not create shared memory segment: Operation not permitted` / `shmget(...)`, including an mmap-configured retry. This is a sandbox kernel restriction, not evidence about PostgreSQL behavior. Per the three-attempt stop rule, no further live-DB bootstrap attempts were made.

## A2 - Application-role grants for installer-created OAuth tables

- **Before:** `mnemos/db_migrations/migrations_v5_4_0_mcp_oauth.sql` created postgres-owned tables without application-role grants when invoked by `mnemos/installer/db.py:632` under the postgres account.
- **After:** `mnemos/db_migrations/migrations_v5_4_0_mcp_oauth.sql:58-71` uses the same guarded `DO`/`pg_roles` pattern as the audit-log migration and grants `mnemos_user` the operations used by `PostgresOAuthStore`: `SELECT, INSERT` on clients/signing keys and `SELECT, INSERT, UPDATE` on authorization codes/tokens.
- **Regression test:** Added `tests/test_migration_lists_sync.py:220-228`, asserting the guarded role check and every required OAuth grant.

## P1 - Durable capped extraction backlog

- **Before:** `mnemos/domain/morpheus/runner.py` selected only `created BETWEEN window_started_at AND window_ended_at` with a hard limit. Unselected rows could age below the next moving lower bound and be stranded permanently.
- **After:** `mnemos/domain/morpheus/runner.py:955-1009` treats `triples_extracted_at IS NULL` as the durable per-row success cursor, removes the moving lower bound, retains the upper bound (`created <= window_ended_at`), and orders deterministically by `created, id` before applying the cap. Successful rows advance durably; capped pending rows remain selectable in later runs; future rows remain excluded.
- **Regression tests:** Added `tests/test_morpheus_extract.py:558-596`, proving old backlog is selected, future rows are not, and a cap of two processes all four rows over two runs even after the replay window start advances past the unprocessed rows.

## T1 - Real MCP SDK SSE tools/list over the wire

- **Before:** The existing integration test replaced `mcp.server.sse`, no-op'd the server runner, called auth internals directly, and inspected `TOOL_REGISTRY`; it could pass with a broken SDK transport or dispatcher.
- **After:** The old test is retained and accurately renamed/documented at `tests/test_mcp_oauth_integration.py:1086-1127` as stubbed auth/session/registry-parity coverage. New test `tests/test_mcp_oauth_integration.py:1130-1185` launches the actual CLI MCP HTTP server on loopback, performs register -> authorize -> token, opens the real `mcp.client.sse.sse_client` transport, initializes a real `ClientSession`, sends the real JSON-RPC `tools/list`, and asserts the response contains at least 20 tools and exactly matches the canonical registry.
- **Regression result:** The real over-the-wire test ran and passed in both the focused set and full suite; it was not skipped.

## E3 - Retry transient or malformed extraction failures

- **Before:** provider timeout/exception/malformed output collapsed to an empty triple list, and the caller marked `triples_extracted_at=NOW()`, permanently treating failure as successful zero-triple extraction. The existing malformed-response test asserted that bug.
- **After:** `mnemos/domain/morpheus/runner.py:43-45` introduces `MorpheusExtractionError`. `mnemos/domain/morpheus/runner.py:1026-1041` catches only that failure category per memory and leaves the marker, sidecar, and processed counter untouched. `mnemos/domain/morpheus/runner.py:1140-1194` distinguishes a valid JSON `[]` success from missing/malformed extraction or verifier output; `mnemos/domain/morpheus/runner.py:1205-1230` returns `None` for provider unavailability/failure.
- **Regression tests:** The former bug-validating assertion was replaced by `tests/test_morpheus_extract.py:327-355`, which proves malformed output leaves the row retryable and a later valid response succeeds. Added explicit valid-empty success (`:358-372`), provider failure (`:375-388`), and malformed/incomplete verifier coverage. The implementation commit message explicitly records that the old test had encoded the bug.

## Test Results

Environment preparation used only the repository's cached packages because outbound dependency resolution was unavailable:

```text
uv pip install --offline --python venv/bin/python -e '.[dev,persephone,sqlite,nats]'
```

Focused regression run:

```text
venv/bin/pytest -q tests/test_mcp_oauth.py tests/test_mcp_oauth_integration.py tests/test_mcp_oauth_live_restart.py tests/test_morpheus_extract.py tests/test_eligibility_predicate.py tests/test_schema_standup.py tests/test_migration_lists_sync.py --tb=short
98 passed, 3 skipped, 2 warnings in 3.60s
```

Exact repository test target:

```text
make test
venv/bin/pytest tests/ -v --tb=short --ignore=tests/test_live_e2e.py
================ 3296 passed, 97 skipped, 86 warnings in 46.96s ================
```

Final static gates:

```text
venv/bin/ruff check <all changed Python files>
All checks passed!

git diff --check
[no output; exit 0]
```

For transparency, an initial dev-only environment stopped collection because the declared Persephone extra was absent. After installing that cached extra, the next run reported `25 failed, 3271 passed, 97 skipped`; all 25 failures were missing declared SQLite/NATS optional dependencies. Installing the cached `sqlite` and `nats` extras produced the final green `make test` result above.

## Review and Push Status

- **Zoder gate:** No approval is claimed. The initial author invocation could not reach its zeroclaw daemon (`socket path does not exist`). Two final `zoder review` attempts reached routing but both failed all retries before any model produced a review because `https://integrate.api.nvidia.com` was unreachable. Ruff, focused regressions, the full suite, and a manual diff review were completed as the documented secondary path.
- **Push attempt:** Executed exactly:

  ```text
  git push https://gitlab.com/ncz-os/mnemos.git HEAD:feat/oauth-mcp-provider-agnostic-v2
  fatal: unable to access 'https://gitlab.com/ncz-os/mnemos.git/': Could not resolve host: gitlab.com
  ```

  The implementation commit remains local because failure occurred at DNS resolution, before authentication.
