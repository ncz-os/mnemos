# Persistence ABC Standardization + Native-Feature Optimization

**Mission start:** 2026-06-04 (STUDIO Claude, autonomous)
**Branch:** `feat/persistence-conformance-test`
**Goal:** Standardize the persistence ABC, conformance-enforce it across all 5
concrete backends, and optimize each backend to its database's native vector /
search features — DB2 12.1.x especially.

## Why

`mnemos/persistence/base.py` defines ~14 repository ABCs (`MemoryRepository`,
`KGRepository`, `FederationRepository`, …) and a `PersistenceBackend` Union of
capability Protocols. Concrete backends (`sqlite`, `postgres`, `mysql`,
`oracle`, `db2`) implement them. Python's ABC machinery only catches a *missing*
method at instantiation; it does **not** catch:

- signature drift between an ABC method and a concrete override,
- a backend that exposes a repo accessor for a capability it does not declare
  (or vice-versa) — a silent-partial backend,
- wrong return types.

No `mypy`/`pyright` gate exists, so this drift is currently unguarded.

## Known drift (pre-work findings)

- **MySQL** exposes `.federation` and `.state_kv` accessors (concrete
  `MysqlFederationRepository`, `MysqlStateRepository`) but declares
  `capabilities = {core}` only — neither `federation` nor `state`. Either the
  capability set is under-declared or those accessors are stubs. Conformance
  test must surface and force a decision.
- MySQL declares no `oauth`/`sessions`/`consultations` accessors (consistent
  with `capabilities = {core}`) — that part is coherent.

## Phases

1. **P1 — Conformance test (`tests/test_persistence_conformance.py`)**, offline,
   no DB. Per importable backend module:
   - every concrete `*Repository` has empty `__abstractmethods__`;
   - each override's `inspect.signature` matches its ABC method (params: names,
     kinds, defaults — annotations/return ignored);
   - **claim ⇒ serve:** for each coarse capability the backend declares, every
     mapped accessor returns an instance of the correct repo ABC;
   - **serve ⇒ claim (finding):** an accessor returning a real repo while the
     capability is undeclared is flagged.
   Oracle/Db2 arms run wherever their drivers import (CI runners); they skip on
   hosts without `oracledb`/`ibm_db`.
2. **P2 — Fix drift** the test surfaces. Make conformance green.
3. **P3 — Native-feature audit/optimization** per backend vector/search path.
4. **P4 — DB2 native-vector deep pass** (priority): `VECTOR`/`VECTOR_DISTANCE`,
   DiskANN `CREATE VECTOR INDEX`, approx/exact mode, dialect native vs compat.

## Verification constraints

- STUDIO has `asyncpg` + `aiosqlite` → SQLite/Postgres logic verifiable in-venv.
- STUDIO lacks `oracledb`/`ibm_db`; DB2 12.1.5 native-vector server is
  unreleased. Oracle/DB2 work is verified at the **SQL-codegen/dialect** layer
  offline (see `tests/test_db2_dialect_parity.py`) and **live-probe-gated** on
  `ORACLE_DSN`/`DB2_DSN`. No live-verified claim is made for Oracle/DB2.

## Gate protocol

Each phase: branch work → codex `adversarial-review` → codex fixes own findings
in place → re-review until `approve` → commit → push ARGONAS→GitLab→GitHub.
