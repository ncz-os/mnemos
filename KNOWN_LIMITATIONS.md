# Known limitations — v6.1

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

---

If you hit one of these in your own deployment, please open an issue at
<https://gitlab.com/ncz-os/mnemos/-/issues> with the specific scenario.
Operational edge cases benefit from real-world reports, not synthetic ones.
