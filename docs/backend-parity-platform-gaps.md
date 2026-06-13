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
