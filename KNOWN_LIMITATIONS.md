# Known limitations — v7.0.0

This file lists known operational caveats that aren't bugs in the strict
sense but are worth surfacing for self-hosting operators. Each entry gives
the trigger conditions and the recovery path, so a deployment that hits one
of these can be unstuck without filing an issue.

## MCP direct-database write quota is process-local

**Where:** `mnemos/mcp/tools/dag.py`.

**Trigger:** MCP tools run inside an in-process MCP server rather than
behind a REST route protected by SlowAPI middleware.

**Symptom:** REST-backed writes are covered by route-layer limits. The
direct `branch_memory` database path keeps a per-user tool guard
(`_mcp_enforce_write_rate_limit`), but that bucket is process-local, so a
deployment running several MCP processes multiplies the effective ceiling
by the process count.

**Recovery:** prefer the REST-backed MCP transport for multi-process
deployments, keep edge and API rate limits enabled, and do not expose
direct-database MCP workers to untrusted clients.

**Proper fix:** a distributed quota bucket keyed by authenticated caller
and tool name, backed by the same shared rate-limit storage as the HTTP
route limiter.

## GDPR right-to-be-forgotten — final-verify race

**Where:** `mnemos/workers/deletion_request_worker.py`.

**Trigger:** all three at once — the target user is actively writing
memories during their own deletion sweep, the deployment runs multiple
worker replicas, and the write lands in the millisecond gap between the
worker's zero-row verify `SELECT` and its `UPDATE deletion_requests SET
status = 'soft_deleted'`.

**Symptom:** the memory committed in that gap keeps `deleted_at = NULL`
while the audit row reports the wipe as complete.

**Recovery:** cancel the completed deletion request and create a new one.
The next sweep picks up the escaped row.

**Proper fix:** a target-scope write fence — an advisory lock keyed on the
target `user_id`, taken by every memory, KG, and session write path while a
covering deletion request is active. That is invasive: every write path in
the codebase would have to consult the fence. The verify-pass loop catches
everything except this final-millisecond window.

## GDPR right-to-be-forgotten — verify-loop exhaustion

**Where:** the same module.

**Trigger:** sustained heavy writes against the deletion target while the
worker sweeps, until the bounded retry exhausts and the request stays in
`status = 'sweep_verifying'`.

**Symptom:** the deletion request is stuck. The worker's dequeue query only
picks up `status = 'confirmed'`, and the admin `cancel` and `restore`
endpoints reject `sweep_verifying`. The active-row partial unique index
also keeps blocking new deletion requests for the same target.

**Recovery:** update the row directly to either re-run or abort:

```sql
-- Re-run: the worker picks this up on its next dequeue.
UPDATE deletion_requests
   SET status = 'confirmed'
 WHERE id = '<deletion-request-uuid>'
   AND status = 'sweep_verifying';

-- Abort: cancels the request. Create a new one if you still want the wipe.
UPDATE deletion_requests
   SET status = 'cancelled'
 WHERE id = '<deletion-request-uuid>'
   AND status = 'sweep_verifying';
```

**Proper fix:** the same write-fence story as the final-verify race. Until
then, bounded retry plus manual recovery is a reasonable shape for
self-hosted MNEMOS, where operators have direct database access.

## Parts of the HTTP surface still require a PostgreSQL pool

**Where:** every call site of
`mnemos.api.persistence_helpers.require_postgres_pool_or_503`.

**Symptom:** the routes below hand-roll PostgreSQL SQL against
`lifecycle.get_pool_manager()` rather than going through the persistence ABC.
Core populates `lifecycle._pool` only for the PostgreSQL profile, so on SQLite,
MySQL, MariaDB, Oracle and Db2 each returns **HTTP 503** with an explanatory
detail. They fail loudly, not silently.

| Module | Routes |
|---|---|
| `api/routes/admin.py` | `POST`/`GET /admin/users`; `POST`/`GET`/`PATCH`/`DELETE /admin/oauth/providers`; `GET /admin/oauth/identities` |
| `api/routes/webhooks.py` | all five: `POST`/`GET /v1/webhooks`, `GET`/`DELETE /v1/webhooks/{id}`, `GET /v1/webhooks/{id}/deliveries` |
| `api/routes/kg.py` | `POST`/`GET`/`PATCH`/`DELETE /v1/kg/triples`, `GET /v1/kg/timeline/{subject}` |
| `api/routes/versions.py` | `POST .../revert/{n}` (`GET .../versions`, `GET .../versions/{n}` and `GET .../diff` are ABC-routed: list/get/contents go through `VersionRepository` — `list_memory_versions`, `get_memory_version` and `fetch_version_contents` — now implemented on all six backends; SQLite writes snapshots in-repo like the Postgres trigger) |
| `api/routes/dag.py` | `GET .../log`, `GET .../branches`, `POST .../branch`, `GET .../commits/{hash}`, `POST .../merge` |
| `api/routes/memories.py` | `GET /v1/memories/{id}?restore=true`, `GET /v1/memories/{id}/compression-manifests`, `POST /v1/memories/rehydrate` |
| `api/routes/kronos.py` | `GET /admin/kronos/anomalies`, `/drift`, `/forecast` |

**Not all of these are the same kind of gap.** KRONOS is PostgreSQL-only *by
design* (see [docs/KRONOS.md](docs/KRONOS.md)). The webhook routes are the
opposite case: a complete `WebhookRepository` ABC is already implemented for all
six backends (`persistence/{postgres,sqlite,mysql,mariadb,oracle,db2}.py`) and
the routes simply do not use it. `POST`/`GET /admin/users` need their own
migrations first — SQLite's `users` table has no `display_name`/`email` and
requires a NOT NULL UNIQUE `username`, and MySQL's has no `created_at`.

**Recovery:** run the `server` profile on PostgreSQL if a deployment depends on
these surfaces. Memory CRUD, search, sessions, the API-key admin routes and —
as of the change listed under *Unreleased* in [CHANGELOG.md](CHANGELOG.md) — the
CHARON `/v1/export` and `/v1/import` routes work on every backend.

**Note:** the machine-generated [docs/BACKEND_PARITY.md](docs/BACKEND_PARITY.md)
tracks capability groups at the persistence layer. It does not track which HTTP
routes bypass that layer, which is what this entry records.

## MCP audit log is written only on PostgreSQL

**Where:** `mnemos/db_migrations/migrations_v5_3_4_mcp_audit_log.sql` and the
SQLite mirror at
`mnemos/db_migrations/migrations_sqlite/migrations_v5_3_4_mcp_audit_log_sqlite.sql`.

**Symptom:** every MCP tool call is logged through the Python logger on all
backends, and additionally persisted to the `mcp_audit_log` table when a
PostgreSQL pool is available. The SQLite schema mirror exists, but the
writer is PostgreSQL-only, so SQLite-only deployments keep the
logger-only surface and have no queryable audit table.

**Recovery:** ship the process logs to your log store, or run the `server`
profile on PostgreSQL if you need queryable MCP audit history.

## SQLite version snapshots cost extra statements per write (NG2-MN-019)

**Where:** `mnemos/persistence/sqlite.py::_write_version_snapshot`,
called from `SqliteMemoryRepository.insert_memory`/`update_memory`.

**Symptom:** an unsuppressed write emits extra statements in the
caller's transaction. The create path (`insert_memory`) emits 7 (1
INSERT + 4 SELECTs + 1 version INSERT + 1 branch UPSERT — no gate
SELECT, since the trigger's INSERT branch snapshots unconditionally).
A versioned-field update (`update_memory`) emits 8 (1 pre-write SELECT
for the IS DISTINCT FROM gate + 1 UPDATE + 4 SELECTs + 1 version INSERT
+ 1 branch UPSERT); a non-versioned write emits 1. Both costs are
pinned by `tests/test_ng2_mn_019_sqlite_versions.py`. The Postgres
equivalent fires server-side in the trigger, so it is invisible to
statement recorders — any recorder/double wrapping SQLite
`insert_memory`/`update_memory` (e.g. the emitted-SQL golden in
`tests/persistence/test_list_paging_tiebreaker_20261103.py`) sees the
extra statements unless the seed insert is wrapped in
`set_suppress_version_snapshot` inside a savepoint scope. The snapshot
and the row update share the caller's transaction, so a snapshot-write
failure rolls both back.

**Recovery:** no operator action needed; this entry exists so the
per-write cost is a known trade-off rather than an accident.

## SQLite version-number derivation is not atomic across concurrent writers (NG2-MN-019)

**Where:** `mnemos/persistence/sqlite.py::_write_version_snapshot`.

**Symptom:** `next_num` is read with
`SELECT COALESCE(MAX(version_num), 0) + 1 FROM memory_versions WHERE
memory_id = ? AND branch = ?` and then INSERTed in a separate statement
inside the caller's transaction. SQLite serializes writers only at
commit granularity, so two concurrent transactions that both read the
same MAX before either commits will both attempt the same `version_num`.
The conflict surfaces as a unique-constraint error on
`(memory_id, branch, version_num)` (or the branch UPSERT) rather than as
a transparent retry. The Postgres analog computes the number inside the
`mnemos_version_snapshot()` trigger under the row lock, so it does not
have this race; an atomic SQLite derivation would require a branch-level
lock that the repository layer does not take.

**Recovery:** a colliding writer receives the constraint error and can
retry (re-reading the MAX inside a fresh transaction); no data is lost
and no duplicate version number is committed. Documented here — and in
the method's inline comment — so the two-statement read/insert is a known
limitation, not an oversight.

## SQLite live-memory visibility renderers still use the bare ``0`` falsifier (NG2-MN-019 follow-up)

**Where:** `mnemos/persistence/sqlite.py` — `_read_visibility_clause`
(line ~425) and the `_render_visibility` READABLE path (line ~606).

**Symptom:** the versions renderers introduced by NG2-MN-019
(`_version_visibility_clause` at line 477, plus the MySQL/Oracle/Db2
versions renderers) use an explicit ``0=1`` boolean literal for an empty
group list, but the pre-existing SQLite live-memory renderers still emit
the bare ``group_clause = "0"`` literal in the same
``AND group_id IS NOT NULL AND {group_clause}`` position. This is
semantically correct on SQLite — an integer is cast to boolean — but it
is inconsistent with the convention now pinned across every other
renderer in the persistence layer and in
`tests/test_db2_dialect_parity.py`.

**Impact:** none observed; SQLite evaluates `0` as false. This entry
documents the residual inconsistency so a future normalization pass does
not mistake the live-memory renderers for already-fixed code. The
versions renderers are the ones exercised by
`test_version_visibility_empty_groups_render_explicit_falsifier` and
`test_version_visibility_nonempty_groups_render_in_clause_all_backends`.

**Recovery:** normalize both live-memory renderers to `0=1` in a
follow-up task; no behaviour change is required on SQLite.

## MySQL/MariaDB live-memory visibility renderers still lack the ACL escape hatch (NG2-MN-019)

**Where:** `mnemos/persistence/mysql.py` — `_render_visibility` (the
live-memory READABLE predicate) and `_version_where_nonroot` (the
pre-NG2-MN-019 live-memory version filter).

**Symptom:** version-history reads now honour per-principal ACL grants
through `memory_acl` (migration 0073, rendering the qualified
`macl.memory_id = memory_versions.memory_id` EXISTS disjunct in
`_version_visibility_clause` — the outer table is referenced by name
because every MySQL version-read call site queries it unaliased). The
live-memory read path does not: its `_render_visibility` renderer
remains owner / world / group + namespace pin, with no ACL EXISTS term.
This is the same asymmetry the other backends maintain between their
live-memory and version-read predicates (ACL grants apply to history
reads but not to the live-memory discovery/list paths that use the live
renderer).

**Impact:** an ACL-granted reader sees the full version history of a
memory they cannot see in the live list/search results. This is
fail-open for history but fail-closed for live discovery; it is a
documented parity gap with the live-memory renderer, not a regression
introduced by NG2-MN-019.

**Migration ordering (0073):** apply
`mnemos/db_migrations/migrations_mysql/0073_memory_acl.sql` BEFORE
upgrading the application code. Every non-root version-history read
(list / get / diff contents) now embeds the `memory_acl` EXISTS term
unconditionally, so an un-migrated MySQL/MariaDB deployment fails closed
with a table-missing error instead of returning the pre-0073 (ACL-less)
result set. The migration is idempotent (`CREATE TABLE IF NOT EXISTS`
with inline index/constraints).

**Recovery:** extend `_render_visibility` to render the ACL disjunct
when `memory_acl` is present (migration 0073 applied), matching the
version-read contract; or document the intentional split in the
security model.

## Memory audit chain is not implemented for MySQL/MariaDB

**Where:** `mnemos/persistence/mysql.py::MysqlBackend.audit_chain` (and the
inherited `MariadbBackend` of the same file) returns `None`. No
`memory_audit_chain` or `memory_audit_roots` migration exists under
`mnemos/db_migrations/migrations_mysql/` or `migrations_mariadb/`.

**Symptom:** MySQL/MariaDB have no signed per-memory audit repository.
`MNEMOS_AUDIT_CHAIN=on` remains best effort; covered mutation paths log an
append failure and preserve the data operation. `MNEMOS_AUDIT_CHAIN=required`
makes covered writes fail and roll back their transaction when the repository,
signing key, or append is unavailable. Federation replication itself does not
require audit when auditing is disabled. Audit proof/health endpoints that
require this capability remain unavailable on these backends.

**Coverage:** required mode covers the ordinary memory CRUD/bulk/import paths,
document import, deduplication, federation replication, and the archive/restore
and deletion-worker paths described in [AUDIT_CHAIN.md](docs/AUDIT_CHAIN.md).
It is not a universal database-write interceptor: MORPHEUS, arbitrary SQL, and
portability restoration do not all have signed-entry enforcement. The existing
signed payload also does not attest every ACL or lifecycle field.

**Recovery:** use a backend with an audit repository when those covered writes
must be signed. PostgreSQL and SQLite mutation rollback behavior has live
integration coverage; Oracle/Db2 definitions require their own live engine
validation. Completing MySQL/MariaDB parity requires audit entry/root tables,
an AuditChainRepository implementation, and sealer/proof integration. The
current required policy refuses unsupported covered writes rather than
claiming that this missing implementation exists.

---

If you hit one of these in your own deployment, please open an issue at
<https://gitlab.com/ncz-os/mnemos/-/issues> with the specific scenario.
Operational edge cases benefit from real-world reports, not synthetic ones.
