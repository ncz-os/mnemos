# MNEMOS OAuth audit fix report — round 2 — 2026-09-09

## Scope and outcome

Work was performed directly in `/tmp/mnemos-audit-clones/mnemos-oauth-fix`
on `feat/oauth-mcp-provider-agnostic-v2`, starting from round-1 report HEAD
`3558f2c` (`7628ef0` is the round-1 code fix). No clone, fetch, merge, deploy,
or push was performed.

| Gap | Outcome | Commit |
|---|---|---|
| 1 / S1 | Closed for one OAuth service instance: aggregate 100/60s ceiling plus protected active-block history | `51a4b8b` |
| 2 / S2 | Closed for the reproduced periodic-key bypass; configured-key entropy remains inherently heuristic | `1dfc462` |
| 3 / A1 | Closed in code and mocked-DSN lifecycle coverage; no live PostgreSQL execution is claimed | `62a002c` |
| 4 / P1 | Closed with durable retry/dead-letter state and bounded-forward-progress regression | `2a3e736` |
| 5 / T1 | Observed environment-blocked skip with an explicit reason; not observed passing in this sandbox | `4b412f1` |

All five commits use `Jason Perlow <jperlow@gmail.com>` and contain no AI
attribution trailer.

## Gap 1 / S1 — aggregate authorization limit and safe eviction

### Before

The authorize and token endpoints shared only one per-caller limiter at 10
attempts per 60 seconds. `_make_room()` evicted the oldest caller without
checking whether its backoff was still active, so caller churn could erase a
live block.

### After

- `mnemos/mcp/oauth.py:43-47` defines a second 100-attempt/60-second ceiling.
- `mnemos/mcp/oauth.py:157-173` evicts only non-blocked entries. If every slot
  is blocked, the new caller is denied with a retry delay and no active history
  is removed.
- `mnemos/mcp/oauth.py:460-473` wires independent per-caller and aggregate
  limiters.
- `mnemos/mcp/oauth.py:485-490` checks the aggregate bucket first, then the
  per-caller bucket, for both authorization and token exchange.
- `tests/test_mcp_oauth_integration.py:1532-1543` proves 100 distinct callers
  consume the aggregate window and caller 101 is denied.
- `tests/test_mcp_oauth_integration.py:1546-1582` proves all-blocked capacity
  fails closed and a mixed bucket evicts the non-blocked entry rather than the
  older blocked entry.

The aggregate bucket is process/service-instance wide, matching the existing
in-process OAuth limiter architecture. It is not a distributed multiworker
rate limiter.

Observed targeted result:

```text
....                                                                     [100%]
4 passed, 1 warning in 0.85s
All checks passed!
```

## Gap 2 / S2 — periodic and common signing keys

### Before

Configured keys were checked for byte length, exact placeholder membership,
and single-character repetition. `"password" * 4` and `"ab" * 16` were at
least 32 bytes and passed construction, allowing predictable JWT forgery.

### After

- `mnemos/mcp/oauth.py:50-71` includes the requested common-word denylist and
  collapses periodic strings to their shortest repeating substring.
- `mnemos/mcp/oauth.py:435-444` rejects every proper repeated-substring key and
  denylist matches after collapse.
- `tests/test_mcp_oauth_integration.py:1447-1486` specifically rejects
  `"password" * 4` and `"ab" * 16`, while accepting a key generated from a
  real `secrets.token_urlsafe(32)` call.
- `.env.example:76-82` recommends leaving the configured key unset with the
  persistent OAuth database so first boot generates and persists
  `secrets.token_urlsafe(32)`; it also gives the same generator for deliberate
  in-memory development. `docs/connectors/chatgpt-pro-developer-mode.md` carries
  the matching operator guidance.
- The existing first-boot implementation is visible at
  `mnemos/mcp/http.py:907-917`.

Residual risk is stated explicitly: repeated-substring and denylist checks are
defense-in-depth heuristics. They cannot prove the entropy of every arbitrary
operator-supplied string.

Observed targeted result:

```text
............                                                             [100%]
12 passed, 1 warning in 0.36s
All checks passed!
```

## Gap 3 / A1 — dedicated OAuth database provisioning

### Before

`MNEMOS_OAUTH_DATABASE_URL` created a dedicated asyncpg pool and immediately
queried `oauth_mcp_signing_keys`. A fresh separate database therefore failed
before any OAuth table was provisioned. Round 1 registered the OAuth migration
only for the default persistence backend.

### After

- `mnemos/persistence/schema.py:223-247` exposes an OAuth-only provisioning
  helper that reuses the existing PostgreSQL render/split/execute path without
  provisioning the core schema or requiring pgvector.
- `mnemos/mcp/http.py:897-926` provisions that dedicated pool before the first
  `PostgresOAuthStore` access. Startup is now inside the cleanup `try/finally`,
  so a provisioning or key failure also closes the pool (`:928-935`).
- `tests/test_mcp_oauth_integration.py:1201-1278` uses the exact
  `MNEMOS_OAUTH_DATABASE_URL` lifespan path with mocked
  `postgresql://mock/oauth`. It asserts all four OAuth `CREATE TABLE`
  statements occur before the signing-key `SELECT`, verifies pool arguments,
  installs the persisted key, and verifies shutdown closure.
- The default backend registration regression remains at
  `tests/test_schema_standup.py:83-99`.

This test uses a mocked DSN and recording asyncpg connection. No live
PostgreSQL server was available or used, so PostgreSQL DDL execution itself is
not claimed.

Observed targeted result:

```text
..                                                                       [100%]
2 passed, 1 warning in 2.30s
All checks passed!
```

## Gap 4 / P1 — persistent extraction poison rows

### Before

`phase_extract()` selected the oldest `triples_extracted_at IS NULL` rows up
to `LIMIT`. `MorpheusExtractionError` left the marker NULL but stored no retry
state, so a persistently failing full batch occupied every future capped run
and newer healthy rows were never attempted.

### After

- `mnemos/core/config.py:574-585` adds
  `MNEMOS_MORPHEUS_EXTRACT_MAX_FAILURES`, default 3 and bounded 1..100.
- `mnemos/domain/morpheus/runner.py:991-1015` left-joins durable failure state
  and excludes only rows in the distinct `dead_letter` state.
- `mnemos/domain/morpheus/runner.py:1036-1092` locks and rechecks the memory row,
  atomically increments consecutive failures, and changes the row to
  `dead_letter` at the threshold. The lock prevents a late failing worker from
  recreating failure state after another worker has marked success.
- `mnemos/domain/morpheus/runner.py:1094-1115` keeps
  `triples_extracted_at` success-only and clears retry state inside the success
  transaction.
- `mnemos/db_migrations/migrations_v5_4_1_morpheus_extract_failures.sql:8-28`
  creates the operator-visible triage table/index and grants the runtime
  `mnemos_user` the required SELECT/INSERT/UPDATE/DELETE operations. A SQLite
  schema mirror is also registered.
- `mnemos/persistence/schema.py:83-87`, the installer migration list, and both
  Compose init lists register the migration. Both existing-volume upgrade
  commands execute it (`docker-compose.yml:218-225` and the staging mirror).
- `tests/test_morpheus_extract.py:642-671` reproduces a cap of 2 with the two
  oldest rows permanently failing. Runs 1 and 2 attempt and dead-letter them;
  run 3 attempts and marks `mem_2` and `mem_3`, proving bounded progress.
- `tests/test_schema_standup.py:102-111` proves backend startup submits the new
  table DDL. Migration-list, Compose-upgrade, role-grant, and late-concurrency
  regressions cover the deployment and race paths.

Dead-lettered rows remain separately queryable in
`morpheus_extract_failures` with attempts, last error, and last failure time;
their success marker remains NULL for manual triage. Deleting/resetting the
sidecar row is the explicit manual retry action.

Observed final targeted result after review fixes:

```text
...............................                                          [100%]
31 passed, 1 warning in 0.71s
All checks passed!
```

## Gap 5 / T1 — real transport observation

The real MCP SDK/SSE test was run before and after the annotation change. This
sandbox cannot bind loopback, so the server process never started and a real
transport pass was not observed.

Before the change, the existing runtime skip reported:

```text
SKIPPED [1] tests/test_mcp_oauth_integration.py:316: loopback bind unavailable for OAuth SSE integration: [Errno 1] Operation not permitted
1 skipped, 1 warning in 0.21s
```

`tests/test_mcp_oauth_integration.py:320-329` now performs the same capability
probe at collection time, and `tests/test_mcp_oauth_integration.py:1142-1145`
adds an explicit `pytest.mark.skipif` with the actual environment reason. On a
loopback-capable host the condition is false and the unchanged subprocess/SDK
test executes.

After the change, the exact observed result was:

```text
s                                                                        [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/test_mcp_oauth_integration.py:1142: loopback bind unavailable for OAuth SSE integration: [Errno 1] Operation not permitted
1 skipped in 0.11s
All checks passed!
```

## Review gate

The required zoder working-tree review was attempted twice. Both attempts
failed before producing a review because the restricted sandbox could not
reach `https://integrate.api.nvidia.com/v1/chat/completions`; zoder correctly
reported `0/1 reviewers completed` and was not represented as an approval.
The permitted Codex secondary review path was then used. Gaps 1, 2, 3, and 5
were approved. Gap 4's first review requested fixes for existing-volume
migration execution, application-role grants, and the late-failure race; all
were fixed, retested, and the second review returned APPROVE.

## Full verification

Command:

```text
make test
```

Observed final output tail (verbatim):

```text
tests/test_worker_lifecycle_backends.py::test_sqlite_admin_lifecycle_repository_covers_crud_archive_restore_and_purge PASSED [100%]

=============================== warnings summary ===============================
venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /private/tmp/mnemos-audit-clones/mnemos-oauth-fix/venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

venv/lib/python3.13/site-packages/starlette/testclient.py:53
  /private/tmp/mnemos-audit-clones/mnemos-oauth-fix/venv/lib/python3.13/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

venv/lib/python3.13/site-packages/typer/params.py:948: 28 warnings
tests/test_cli_typer_click_compat.py: 1 warning
  /private/tmp/mnemos-audit-clones/mnemos-oauth-fix/venv/lib/python3.13/site-packages/typer/params.py:948: DeprecationWarning: The 'is_flag' and 'flag_value' parameters are not supported by Typer and will be removed entirely in a future release.
    return OptionInfo(

tests/domain/headroom/test_json_minify.py::test_json_minify_preserves_edge_numeric_lexemes_exactly[0]
  /private/tmp/mnemos-audit-clones/mnemos-oauth-fix/tests/conftest.py:73: DeprecationWarning: There is no current event loop
    asyncio.get_event_loop()

tests/test_kronos.py: 2 warnings
tests/test_worker_lifecycle_backends.py: 51 warnings
  /private/tmp/mnemos-audit-clones/mnemos-oauth-fix/venv/lib/python3.13/site-packages/aiosqlite/core.py:63: DeprecationWarning: The default datetime adapter is deprecated as of Python 3.12; see the sqlite3 documentation for suggested replacement recipes
    result = function()

tests/test_mcp_oauth_integration.py::test_attacker_signed_jwt_is_rejected_by_sse_gate
  /private/tmp/mnemos-audit-clones/mnemos-oauth-fix/venv/lib/python3.13/site-packages/jwt/api_jwt.py:147: InsecureKeyLengthWarning: The HMAC key is 12 bytes long, which is below the minimum recommended length of 32 bytes for SHA256. See RFC 7518 Section 3.2.
    return self._jws.encode(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
================ 3307 passed, 97 skipped, 86 warnings in 42.79s ================
```

Ruff was run on every changed Python/test file after each gap and reported
`All checks passed!`; `git diff --check` was clean before every commit.

## Worktree and push state

No push was performed. The branch contains the five round-2 commits listed
above, in order. Two unrelated modified files appeared concurrently during
the work and were deliberately excluded from every commit:

```text
 M mnemos/domain/compression/judge.py
 M tests/test_judge.py
```

The supplied `ASTRA_VERIFICATION_2026-09-09.md` remains untracked and was not
modified or committed. The full `make test` result above includes the current
working tree, including those unrelated modifications; it passed.
