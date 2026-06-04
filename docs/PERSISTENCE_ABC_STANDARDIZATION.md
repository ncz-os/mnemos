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
  candidate set). **DB2 shipped** (P4 below); sqlite (recency currently a no-op)
  and mysql/oracle (SQL-side recency that bypasses ANN index ordering) remain —
  the mysql/oracle strategy switch is live-unverifiable and changes ordering, so
  it needs a design decision before changing.

### P4 (DB2 native-feature deep pass)
- **`semantic_search` recency over-fetch** — shipped (see P3); DB2 now widens the
  DiskANN candidate fetch under `boost_recency` and re-ranks safely
  (`_rank_score_sort_key` sorts NULL/invalid/non-finite distances last so a
  degraded row can never displace a valid candidate). Offline-tested.
- **`fts_search` native text index** — shipped. DB2 full-text search used a
  non-indexed `UPPER(content) LIKE '%term%'` full-table scan even where a Db2
  Text Search index exists (the code comment acknowledged the gap). Added an
  opt-in `MNEMOS_DB2_TEXT_SEARCH=contains` mode (`config.db2_text_search_override`
  + `_resolve_db2_text_search_mode`) emitting the native `CONTAINS(content, ?) = 1`
  predicate that engages the Db2 Text Search index. Default stays `like` (safe on
  stock Db2; `CONTAINS` raises SQL20424N without a text index, so it must be
  opt-in). Offline dialect tests assert the predicate per mode + invalid-mode
  fallback. Live behaviour gated on `DB2_DSN` + a provisioned text index.
- **`Db2SessionsRepository.create_session` conflation** — still open; design
  blessed, mechanics need operator sign-off (auth path, DB2 not live-testable).

  Accurate structure (verified): the browser/OAuth-session helpers
  (`create_session(session_id, expires_at, metadata)`, `lookup_session`,
  `update_session_active`, `expire_session`) live as **facade-level methods on
  `OracleBackend`** (oracle.py:4556-4618) — NOT in `OracleSessionsRepository`
  (chat, oracle.py:2751) nor `OracleOAuthRepository` (oauth tokens, 2705). DB2
  regressed this: it put the browser-session methods inside
  `Db2SessionsRepository` (shadowing the chat `create_session`) and the
  `Db2Backend` facade delegates `lookup_session`/`update_session_active`/
  `expire_session` to `self._sessions_repo` (db2.py:4037-4043).

  **GRAEAE verdict (architecture_design, consensus 1.0, winning muse gemini):**
  browser/OAuth-session ops must live solely on the auth surface; chat
  `SessionsRepository` must exclusively own conversational state. DB2 should
  drop the `Db2SessionsRepository.create_session` override (inherit the chat
  one) and the browser-session helpers should be reached via the facade's
  Oracle-inherited path, not the chat repo. Migration is static-analysis +
  conformance + mocked-driver tests only until DB2 12.1.5 is live.

  **Why not auto-applied:** multi-file auth-path refactor on a backend with no
  live test; the correct relocation target is the `OracleBackend` facade layer
  (GRAEAE's prompt framing assumed an OAuth repo), so the exact rewire wants
  operator confirmation. Tracked by `KNOWN_SIGNATURE_DRIFT`.

### Item 1 SHIPPED — DB2 session-ownership refactor (live-verified)
Applied the GRAEAE separation: the 7 browser/OAuth-session members moved from
`Db2SessionsRepository` onto the `Db2Backend` facade (mirroring `OracleBackend`);
`Db2SessionsRepository` is now a pure chat repo; `KNOWN_SIGNATURE_DRIFT` entry
removed; ownership-boundary regression test added
(`tests/test_db2_session_ownership.py`). Live-verified on the CERBERUS DB2 EAP
container (`db2://…@192.168.207.96:50001/MNEMOS`): 179 passed / 7 skipped across
the db2 live + conformance + dialect suites.

**Newly exposed pre-existing gap (operator decision):** the chat
`SessionsRepository` write path (`create_session`, `add_message`,
`add_memory_injections`, `update_metrics`) is `raise NotImplementedError` in
**both** `OracleSessionsRepository` and (now, by inheritance)
`Db2SessionsRepository` — only the read methods (`get_session`,
`fetch_history`) are implemented. Yet Oracle and DB2 both **declare** the
`sessions` capability. The refactor did not cause this (DB2 previously *masked*
it with the misplaced browser `create_session`); it surfaced it, and made the
failure honest (clean `NotImplementedError` instead of wrong browser semantics).
Decision needed: implement chat-session writes on the enterprise backends, or
drop `sessions` from their declared capabilities (chat sessions may be a
Postgres/SQLite-only feature). Conformance gate stays green either way (signature
+ accessor-type pass); this is a capability-honesty / product-scope call.

### Item 2 SHIPPED — recency standardized across all 5 backends
All backends now use the ANN-index-friendly pattern (bare distance/similarity
ORDER BY → over-fetch `candidate_limit = max(limit, min(limit*4, 200))` →
Python re-rank → cap to limit), with uniform conservative date resolution
(`updated → created → date.min`) and an invalid-score sort key (missing/non-finite
sorts last). sqlite (was a no-op), mysql + oracle (were SQL-side, defeating the
vector index) converted; postgres + db2 already used it (db2 gained the
over-fetch + a corrupt-date fix). Verified: sqlite offline (aiosqlite); db2 live
on CERBERUS DB2 EAP; oracle offline + partial-live (CDB read-only blocked writes);
mysql offline dialect (VECTOR_DISTANCE is HeatWave-only).

### Item 3 SHIPPED — all MySQL stub surfaces implemented + live-verified
Every `_stub_method` stub in `mnemos/persistence/mysql.py` replaced with a real
MySQL 9 implementation ported 1:1 from the SQLite reference: **State, KG,
Versions, Branches, Compression, ConsultationAudit, Federation** (+ inline DDL
for each table, applied in `open()`). `mysql.py` now has zero `_stub_method`
references. Capabilities advertise `{core, state, federation}`; the detail set is
`{memory_crud, vector_search, fts, kg, versions, state, branches, compression,
federation}`. The conformance gate's `KNOWN_UNDECLARED` allowlist is now **empty**
— every declared MySQL capability is genuinely served. Each surface has a
`MYSQL_DSN`-gated live round-trip test; **all 7 verified live (7 passed) against a
MySQL 9.0.1 container on CERBERUS** (`:3307`). MySQL vector *search* itself
remains HeatWave-only (Community lacks `VEC_DISTANCE`), so the vector path is
covered offline at the SQL-shape layer.

## Open items for operator / next session
1. **Chat-session writes on enterprise backends (Item 1 follow-up)** — `create_session`
   / `add_message` are `NotImplementedError` in Oracle + DB2 (chat
   `SessionsRepository`) though both declare `sessions`. Implement chat-session
   writes on the enterprise backends, or drop the `sessions` capability claim.
2. **Oracle live-write verification** — blocked by the CERBERUS Oracle container
   CDB being open READ ONLY (`ORA-65054`); the recency conversion is verified by
   inspection + offline + partial-live (reads) + db2-parity. Reopen the CDB
   read-write (operator-owned container) to run the Oracle write suite.
3. **MySQL vector search** — needs a HeatWave (or Enterprise) MySQL to live-test
   `VECTOR_DISTANCE`; Community 9.0.1 stores `VECTOR` but lacks the distance fn.
