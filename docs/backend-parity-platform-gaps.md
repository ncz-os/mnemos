# Backend parity — platform gaps, intentional differences, and gated work

Companion to `backend-parity-gap-matrix.md` (which is script-generated tables and
gets overwritten on regen — keep narrative here). Backends: oracle, postgres,
db2, mysql, sqlite against the 14 persistence ABCs / 111 ABC methods.

Current coverage (IMPL / STUB / RAISE / absent):
- postgres 111 / 0 / 0 / 0  — reference, full
- sqlite  110 / 0 / 1 / 0  — near-full (1 raise: a postgres-only path)
- mysql   102 / 1 / 8 / 0  — webhook + consultations closed 2026-06-13
- db2      94 / 1 / 0 / 16 — consultations native 2026-06-13; rest inherits oracle
- oracle   93 / 14 / 4 / 0 — sessions/consultations stubs + oauth raises (gated)

## Why the consultations surface diverged (root cause, fixed for db2)

The GRAEAE consultation tables (`graeae_consultations`, `graeae_audit_log`,
`consultation_memory_refs`) gained `owner_id` / `namespace` (ownership) and
`deleted_at` (soft-delete) AFTER the original Oracle port. The canonical schema
(sqlite `migrations.sql` + the mysql parity port `0002_feature_parity_schema.sql`)
carries them; `db/migrations_oracle/0002_graeae.sql` predates both features and
omits them. OracleConsultationsRepository's read methods filter
`c.owner_id` / `c.namespace` / `deleted_at`, so on Oracle they never had a schema
to run against — which is why Oracle left them as stubs and db2 (inheriting
Oracle) had them fenced/absent. Fixed for db2 by `0002c_graeae_parity_cols.sql`
(adds the missing columns) + native overrides. Oracle still needs the same column
migration + un-stubbing, but that is **gated** (no live Oracle XE to validate).

mysql looked like it was "missing" the 4 consultation reads, but they were
inherited from `MysqlConsultationAuditRepository` (real impls) — the gap matrix
only credits methods declared on the `*ConsultationsRepository` class. Added
explicit super()-delegators so the surface is visible + counted.

## Intentional platform differences (NOT gaps — do not "fix")

- **`set_suppress_version_snapshot`** — STUB (no-op) on oracle/db2/mysql; IMPL on
  postgres (`SET LOCAL mnemos.suppress_version_snapshot`) + sqlite (temp
  `mnemos_tx_flags`). It is a NON-abstract ABC method (default `...`). postgres
  and sqlite create version snapshots via a DB-side trigger that reads a
  suppress flag; oracle/db2/mysql do versioning in application code with no
  suppressible DB trigger, so there is nothing to suppress — the no-op is the
  correct backend-appropriate behavior, not a missing feature.

## Genuine platform gaps (need a newer engine / are inherently partial)

- **sqlite 1 RAISE** — a postgres-specific path with no sqlite equivalent;
  sqlite is the edge backend and is expected to be partial.

## mysql 8 RAISE — NOT VECTOR; portable MemoryRepository methods (NEXT SLICE)

Correction (2026-06-13): the mysql RAISE set is **not** VECTOR/embedding — those
already degrade to a python-cosine `semantic_search` impl. The 8 ABC-counted
RAISEs are all `MysqlMemoryRepository` versioning / ACL / dedup methods that were
simply never ported. They ARE portable from the postgres reference and
validatable on a live MySQL container (mysql9 used for the next slice; mysql8
also works — none of these need VECTOR). All schema deps exist in
`db/migrations_mysql` (memory_versions, memory_branches; memories.content_hash /
provenance / morpheus_run_id / source_memories / federation_source). Split by risk:

Low-risk (ACL-free, port + validate straightforwardly):
- `fetch_referenced_memory_allowlist` — `id = ANY($1::text[])` -> `IN (%s,…)` + optional owner/ns scope.
- `fetch_memory_export` — memories SELECT w/ optional owner/ns/category filters + the provenance columns; `$idx`/`$idx+1` LIMIT/OFFSET -> `%s`.
- `find_duplicate_content_groups` — GROUP BY owner/ns/content_hash HAVING COUNT>1. GOTCHA: postgres `ARRAY_AGG(id ORDER BY created,id)` + `[1]` canonical -> MySQL `GROUP_CONCAT(id ORDER BY created,id)` (MUST raise `group_concat_max_len`, default 1024 truncates large groups) split to a list, canonical via `SUBSTRING_INDEX(...,',',1)`; or `JSON_ARRAYAGG` (ordering not guaranteed — prefer GROUP_CONCAT + explicit max_len).
- `consolidate_duplicate_memories` — UPDATE … SET consolidated_into/consolidated_at/deleted_at WHERE `id = ANY($2::text[])` -> `IN (%s,…)`; `NOW()` -> `NOW(6)`; return rowcount.

Version-ACL cluster (do together — share the version-visibility predicate):
- `assert_memory_readable`, `fetch_memory_log` (RECURSIVE CTE over memory_versions+memory_branches), `fetch_diff_commit_pair`, `fetch_checkout_commit`. Postgres uses `_core_read_visibility_predicate` / `_core_version_visibility_predicate` (emit `$n`); mysql has `_render_visibility(VisibilityFilter, table_alias)` (emit `%s`) — needs a version-table (`mv` alias) visibility rendering path. Build the VisibilityFilter from UserContext the way the existing mysql get_memory/list_memories do; root path is a simple existence check (no predicate).

NOTE: `fetch_duplicate_content_groups` (mysql L1539) is a vestigial mysql-only
stub NOT on the ABC — not a parity requirement, leave or delete.

## Gated work (needs live infra or an architectural decision — DO NOT blind-build)

- **oracle Sessions + Consultations stubs (14) + oauth raises (4)** — needs a live
  Oracle XE to port + validate. A blind hive worker hallucinated Oracle APIs here
  (ACL_READ_BIT / BITAND) once; only catchable with a live DB.
- **oauth token-hash → OIDC split** — oracle/db2 `0004` oauth schema is a
  token-hash design (id-PK / client_secret_hash / auth_url / scopes); postgres
  `migrations_v3_oauth.sql` is OIDC (name-PK / kind / issuer_url / client_secret /
  authorize_url / userinfo_url) which `core/oauth.py build_client` requires. These
  are incompatible; reconciling them is a data-migration + plaintext-secret
  decision for a human (mem_1781325370745_d6cba6).
- **db2 sessions drift** — db2 inherits Oracle session stubs; needs the same
  treatment as oracle sessions (gated on the oracle work + an Oracle/DB2 session
  schema decision).

### UPDATE 2026-06-13 — 4 of the 8 mysql RAISE ported

Done (commit 784bb20, validated MySQL 9.0): fetch_referenced_memory_allowlist,
fetch_memory_export, find_duplicate_content_groups, consolidate_duplicate_memories.
mysql is now 106 IMPL / 4 RAISE.

Remaining 4 = the version-ACL cluster: assert_memory_readable, fetch_memory_log,
fetch_diff_commit_pair, fetch_checkout_commit. These need a **MySQL-dialect
visibility predicate** first. Postgres uses `mnemos.core.visibility.
read_visibility_predicate` / `version_visibility_predicate`, which emit `$n`
placeholders AND a `group_id = ANY($groups)` array param. A naive `$n -> %s`
rewrite breaks on the array param (MySQL needs `group_id IN (%s, ...)` expansion).
So the slice must author a `%s`/list-expanding MySQL variant that mirrors the RLS
branch logic EXACTLY (owner_id / federation_source IS NOT NULL / world bits
`permission_mode % 10 >= 4` / group bits `(permission_mode/10)%10 >= 4 AND
group_id IN (...)`), then port the 4 methods (fetch_memory_log is a RECURSIVE CTE
over memory_versions+memory_branches; root paths skip the predicate). This is
security-sensitive (RLS read policy) — do it as a focused, heavily-reviewed slice,
not a quick port. mysql8 OR mysql9 both validate it (no VECTOR dep).

### FINAL 2026-06-13 — all non-gated parity reached

mysql is now 110 IMPL / 1 STUB / 0 RAISE / 0 absent — the version-ACL cluster
(assert_memory_readable, fetch_memory_log, fetch_diff_commit_pair,
fetch_checkout_commit) landed in commit 311cea5, security-validated on MySQL 9.0
(deny paths + the fail-closed version-vs-live group behavior). The remaining 1
STUB is set_suppress_version_snapshot (correct-by-design no-op).

Cross-backend status: postgres 111 (reference), sqlite 110 (1 raise = a
postgres-only path; edge backend, expected partial), mysql 110 (COMPLETE for all
non-gated work), db2 94 (consultations native; the 16 absent + 1 stub are
oracle-inherited sessions/oauth — GATED), oracle 93 (GATED, needs live Oracle XE).

Everything still open is GATED and must NOT be blind-built: oracle Sessions +
Consultations (live Oracle XE), the oauth token-hash -> OIDC schema split (data
migration + plaintext-secret decision, mem_1781325370745), db2 sessions drift +
db2 oauth (the oracle parent must resolve the session/oauth design first).

### UPDATE 2026-06-13 (ext) — oracle Consultations un-gated; sessions gate proven real

Stood up Oracle 23ai Free (gvenzl/oracle-free:23-slim) — the oracle "needs a live
DB" gate was only that. Oracle XE 21c does NOT work (the oracle migrations use
`CREATE TABLE IF NOT EXISTS`, a 23c+ feature). With 23ai up:
- oracle GRAEAE Consultations implemented natively (6 read methods + the
  create_consultation_with_audit that 0041 unblocked) + migrations
  0041_graeae_parity_cols + 0042_model_registry. oracle 93 -> 99 -> 100 IMPL
  (incl. fetch_model_provider). Validated on 23ai.

Final coverage: postgres 111, sqlite 110, mysql 110, db2 94, oracle 100 — all
non-gated parity reached.

The chat-Sessions gate is NOT a DB-availability gate — it is a genuine SCHEMA
CONFLICT, now proven against the live DB: the oracle `sessions` table is an
AUTH/token schema (`session_id RAW`, `started_at`, `expires_at`,
`last_active_at`, `metadata`) used by the auth `get_session`, NOT the
chat-session schema the `SessionsRepository` ABC expects (model, message_count,
total_tokens, last_activity + `session_messages` with model/tokens_used/
memories_injected + `session_memory_injections` with message_id/relevance_score).
Same table name, two different purposes across backends. Implementing chat
sessions on oracle/db2 needs a human decision: new chat-session tables, or rename
the auth table, or extend it. db2 inherits the same conflict.

OAuth stays gated for the same reasons as before (token-hash vs OIDC, breaking
schema + plaintext client_secret + prod-data migration).
