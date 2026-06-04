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

## Findings log

### P1 (shipped, commit on `feat/persistence-conformance-test`)
- `fetch_model_recommendation.quality_floor` was `0.7` on sqlite + db2 vs the
  ABC/postgres contract `0.85` — silent KNEMON model-routing divergence.
  **Fixed**: conformed both impls to `0.85`.
- Conformance gate added (`tests/test_persistence_conformance.py`), hardened
  across 3 codex review rounds (param-kind compatibility incl. positional-order,
  `compression_queue` coverage, accessor-specific ABC in serve⇒claim, narrowed
  optional-driver import/construction skips). 34 tests, ruff-clean.

### P2 (decisions)
- **MySQL `federation` / `state_kv` accessors (KNOWN_UNDECLARED)** — resolved by
  design, no code change. `MysqlFederationRepository` / `MysqlStateRepository`
  are pure `_stub_method` stubs (every method raises `NotImplementedError`);
  the backend correctly declares `capabilities = {core}` and does **not** claim
  `federation` / `state`. The accessors return stub repos only to keep a uniform
  facade shape; they do not *serve* the capability. Callers do not consistently
  guard via `has_capability`/`require_capability`, and the
  `mnemos/federation/nats_consumer.py` path reaches `backend.federation`
  unconditionally — so converting the accessor to raise `BackendCapabilityMissing`
  is low value and mildly risky. Allowlist retained with this rationale.

- **DB2 `create_session` conflation (KNOWN_SIGNATURE_DRIFT) — needs design
  decision; deferred.** Two distinct "session" concepts exist:
  - *chat sessions* — the `SessionsRepository` ABC (`create_session(user_id,
    namespace, model, initial_context)`, `add_message`, `add_memory_injections`,
    `update_metrics`, …). `OracleSessionsRepository` implements this correctly.
  - *OAuth/browser sessions* — `create_session(session_id, expires_at,
    metadata) -> str` plus the **non-ABC** helpers `lookup_session` /
    `update_session_active` / `expire_session`. These are owned by the OAuth
    surface (`OAuthRepository.create_session`/`revoke_session`/…) and used
    internally by an OAuth-session create flow (oracle.py:4586, sqlite.py:4045,
    db2.py:3679).

  `Db2SessionsRepository(OracleSessionsRepository)` wrongly **overrides**
  `create_session` with the OAuth-session signature and carries the OAuth-session
  helpers, shadowing the inherited-correct chat `create_session`. DB2 also
  declares the full capability set incl. `sessions`, so claim⇒serve requires a
  working chat `create_session` it does not provide.

  This is a cross-backend ownership reconciliation (which class owns
  browser-sessions), not a mechanical fix, and DB2 is not live-verifiable on
  STUDIO (`ibm_db` absent; DB2 12.1.5 unreleased). **Open question for GRAEAE /
  operator:** should browser-session ops live solely on the OAuth surface, with
  `Db2SessionsRepository` dropping its override to inherit the chat
  `create_session`? Tracked by the `KNOWN_SIGNATURE_DRIFT` allowlist entry.

### P3 (native-feature standardization)
- **Recency-boost candidate over-fetch** — `postgres.semantic_search`
  over-fetches `candidate_limit = max(limit, min(limit * 4, 200))` before the
  Python recency re-rank, then caps to `limit`, so recency can promote an item
  ranked just outside the top-`limit` by pure vector distance. sqlite / mysql /
  oracle / db2 fetch exactly `limit` then re-rank within it → their recency
  boost is strictly weaker and inconsistent across backends. Standardize all
  four on the postgres over-fetch pattern (keeps each engine's native vector
  scan — incl. DB2 `FETCH APPROX FIRST` DiskANN — engaged on the larger
  candidate set).
