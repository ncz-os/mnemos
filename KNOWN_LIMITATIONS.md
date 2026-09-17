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
| `api/routes/versions.py` | `GET .../versions`, `GET .../versions/{n}`, `GET .../diff`, `POST .../revert/{n}` |
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
