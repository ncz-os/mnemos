# PLAN FB98 — mnemos Oracle/Db2 lifecycle SQL (findings P1, P2, S1, S4)

Base: master (0873b0e1). Branch: `freebuff/FB98-20261102`.

## Verified at head (re-checked, not taken from the audit)
- P1 `active_deletion_for_scope` (`worker_lifecycle.py:~770`) appends a literal `LIMIT 1` for every
  dialect. Oracle rejects LIMIT (ORA-00933); Db2 only accepts it under a compatibility vector.
  Caller catches `Exception`, so the write fence silently fails OPEN on those backends.
- P2 `_hard_delete_identity_rows` revokes with `revoked = TRUE, last_used = NULL` and deletes
  oauth_sessions / oauth_identities / user_groups by `owner_id` (+ `AND namespace = ?`).
  migrations_oracle/0003_api_keys.sql has `revoked_at` / `last_used_at`; 0005/0006/0008 key on
  `user_id` and have no `namespace`. So every Oracle/Db2 erasure raises ORA-00904 / SQL0206N.
- S1 `sweep_for_archival` builds `SELECT id FROM (<ordered>) WHERE ROWNUM <= ? FOR UPDATE SKIP
  LOCKED` for oracle+db2 — the exact ORA-02014 shape the `_claim` comment says was measured
  failing on production 26ai. Db2 also wants `SKIP LOCKED DATA`.
- S4 `_Ops.sql` types the `? IS NULL` marker only for Postgres; Db2 can raise SQL0418N on an
  untyped marker in IS NULL. `_scope()` and P1's query both use that shape.

## Files
- `mnemos/persistence/worker_lifecycle.py` (only production file touched).
- `tests/test_worker_lifecycle_oracle_db2_sql.py` (new): SQL-shape tests with fake Oracle/Db2
  backends that record every statement; no live database.

## Tests (red first, per finding)
1. P1: fence SQL for oracle/db2 must not contain `LIMIT`; must end with a row limiter
   (`FETCH FIRST 1 ROWS ONLY`) and still ORDER BY the same expression. Postgres/SQLite keep LIMIT.
2. P2: revoke statement for oracle/db2 sets `revoked_at`/`last_used_at` and never `revoked =`;
   deletes of oauth_* / user_groups use `user_id` with no `namespace` predicate; api_keys keeps
   owner_id + namespace. Postgres shape unchanged.
3. S1: archive claim for oracle must not combine ROWNUM with FOR UPDATE (keep the id predicate
   shape), db2 must spell `SKIP LOCKED DATA`, and both must limit with FETCH FIRST.
4. S4: db2 rendering of `? IS NULL` is `CAST(? AS VARCHAR(256)) IS NULL`; postgres still `?::text`;
   sqlite unchanged.

## Risks / limits
- No Oracle or Db2 instance here: these are shape assertions, plus the file's existing SQLite /
  Postgres behaviour must stay byte-identical. Say so plainly in the summary.
- P3 (LOB materialisation) belongs to FB99 and is deliberately not touched here.

## Loop
`python3 -m pytest tests/test_worker_lifecycle_oracle_db2_sql.py tests/test_worker_lifecycle_backends.py tests/test_worker_lifecycle_postgres.py -q`
plus `ruff check` on the changed files; then branch review (`zoder review`) and push to ARGONAS.
