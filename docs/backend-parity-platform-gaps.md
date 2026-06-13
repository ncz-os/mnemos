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

- **mysql 8 RAISE** — VECTOR/embedding methods (semantic_search and the embedding
  write/duplicate-content paths). MySQL `VECTOR` / `TO_VECTOR` / `VECTOR_DISTANCE`
  are MySQL **9.0** features; MySQL 8.0 cannot host them. These are gated on a
  MySQL 9.0 target (the prod engine) — implement + validate there, not on 8.0.
  The 8.0 `MysqlBackend.open()` probe intentionally fails the VECTOR DDL.
- **sqlite 1 RAISE** — a postgres-specific path with no sqlite equivalent;
  sqlite is the edge backend and is expected to be partial.

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
