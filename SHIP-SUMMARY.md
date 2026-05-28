# KNEMON Recommender Task-Type Split

Implemented hive `019e6db2` follow-up for `/v1/providers/recommend` task-type differentiation on branch `feat/knemon-mvp`.

## Shipped

- Tightened `mnemos/domain/pantheon/recommendation.py` with explicit task policy for coding, narrative, reasoning, embedding, routing, and web search.
- Added capability requirements, model-family preferences, Opus exclusion for narrative, chat exclusion for embeddings, and cost-tier ceilings with fallback tiers.
- Removed Opus as a coding preference; coding now prefers coder/deepseek/sonnet families and only falls to tier C when no tier A/B model is available.
- Added regression coverage proving task types no longer collapse to one model and narrative does not choose Opus when Sonnet is available.

## Verification

- `.venv/bin/pytest -q tests/domain/test_knemon_recommender.py tests/test_knemon_triage_llm.py` passed: 17 passed.
- `python3 -m py_compile mnemos/domain/pantheon/recommendation.py tests/domain/test_knemon_recommender.py`
- `git diff --check -- mnemos/domain/pantheon/recommendation.py tests/domain/test_knemon_recommender.py`
- Smoke curl against local FastAPI on a temporary seeded SQLite registry:
  - `code-fix` -> `nvidia/qwen/qwen3-coder-480b-a35b-instruct`
  - `narrative` -> `anthropic/claude-sonnet-4-6`
  - `reasoning` -> `anthropic/claude-opus-4-6`
  - `embedding` -> `mnemos-local/bge-m3`
  - `routing` -> `groq/llama-3.1-8b-instant`
  - `web_search` -> `perplexity/sonar`

## Artifacts

- Work log: `/tmp/knemon-recommender-v2/codex-out.log`

No redeploy performed.

---

# Rust Federation JSON Serializer

Implemented hive `019e6d13-0597` federation feed JSON serialization port on branch `feat/knemon-mvp`.

## Shipped

- Added `mnemos-rust-ext/src/federation.rs` with a PyO3-exposed, simd-json/serde-backed compact JSON serializer for federation memory rows.
- Exposed `mnemos_native_search.serialize_memory_for_feed(rows)` from the existing native extension crate.
- Added `mnemos/domain/federation/native_bridge.py` with native import and stdlib `json` fallback.
- Added parity/fallback/native tests in `tests/domain/test_native_federation.py`.
- Kept the existing `mnemos/domain/federation.py` module import-compatible while allowing additive federation submodules.

## Verification

- `cargo test --manifest-path mnemos-rust-ext/Cargo.toml`
- `.venv/bin/python -m pytest tests/domain/test_native_federation.py tests/domain/test_native_search.py`

## Benchmark

Recorded in `/tmp/rust-federation/codex-out.log`.

- 10k mixed rows with datetimes and optional embeddings: native 138k rows/s, stdlib fallback 195k rows/s, byte parity true.
- 10k 1KiB feed rows: native 230k rows/s, stdlib fallback 181k rows/s, 1.27x speedup, byte parity true.

No redeploy performed.

---

Db2 OAuth/sessions/consultations persistence

Implemented:
- Added Db2-native OAuth token/state, protocol session, session event, consultation, and consultation response methods in `mnemos/persistence/db2.py`.
- Added Db2Backend facade overrides so the new protocol methods use native Db2 SQL instead of Oracle `RETURNING ... INTO`.
- Updated Db2 capability advertising to include core, oauth, sessions, consultations, federation, audit, and state.
- Reworked `db/migrations_db2/0038_oauth_sessions_consultations.sql` into idempotent Db2 dynamic compound statements guarded by `SYSCAT.TABLES`, `SYSCAT.COLUMNS`, and `SYSCAT.INDEXES`.
- Added `tests/persistence/test_db2_oauth_sessions_consultations.py` for SQLite parity shape, Db2 method/capability checks, migration assertions, and native positional SQL checks.
- Updated capability protocol tests for full Db2 capability advertising.

Live Db2:
- Applied `0038_oauth_sessions_consultations.sql` to CERBERUS Db2 EAP at `192.168.207.96:50001/MNEMOS`.
- Verified tables: `CONSULTATIONS`, `CONSULTATION_RESPONSES`, `OAUTH_STATE`, `OAUTH_TOKENS`, `SESSIONS`, `SESSION_LOGS`.
- Verified new `sessions` columns: `SESSION_ID`, `STARTED_AT`, `LAST_ACTIVE_AT`, `EXPIRES_AT`, `METADATA`.
- Ran a live OAuth/session/consultation roundtrip through `Db2Backend`; cleaned up `codex_db2_test` rows afterward.

Db2 dialect note:
- Live Db2 rejected `BLOB` for keyed/equality protocol ids (`SQL0350N`), so the migration uses `CHAR(n) FOR BIT DATA` for indexed binary ids. Runtime values remain bytes.
- Live Db2 rejected `IS JSON` check constraints in this DDL path, so JSON payloads are stored as CLOB and parsed by the backend without Db2 check constraints.

Verification:
- `.venv/bin/python -m py_compile mnemos/persistence/db2.py tests/persistence/test_db2_oauth_sessions_consultations.py tests/persistence/test_capability_protocols.py`
- `.venv/bin/pytest -q tests/persistence/test_db2_oauth_sessions_consultations.py tests/persistence/test_capability_protocols.py` passed: 8 passed.
- Live migration idempotency rerun passed: 16/16 statements OK.

Artifacts:
- Work log: `/tmp/db2-backend-impl/codex-out.log`

---

Rust vector similarity extension

Implemented:
- Added `mnemos-rust-ext/` PyO3 crate exposing `mnemos_native_search`.
- Implemented SIMD-backed cosine and batch cosine helpers with Python-compatible invalid-vector semantics.
- Added zero-copy NumPy `float32` fast paths for batch retrieval workloads, while keeping Python list inputs supported.
- Added `mnemos/domain/search/native_bridge.py` adapter with pure-Python fallback.
- Added `scripts/bench_native_search.py` for native-vs-Python timing.
- Added focused parity/fallback tests in `tests/domain/test_native_search.py`.

Build:
- Development: `cd mnemos-rust-ext && maturin develop`
- Release: `cd mnemos-rust-ext && maturin build --release`

Verification:
- `cd mnemos-rust-ext && PYO3_PYTHON=/Users/jasonperlow/Projects/mnemos-prod-working/.venv/bin/python cargo check`
- `cd mnemos-rust-ext && PYO3_PYTHON=/Users/jasonperlow/Projects/mnemos-prod-working/.venv/bin/python cargo test`
- `cd mnemos-rust-ext && PYO3_PYTHON=/Users/jasonperlow/Projects/mnemos-prod-working/.venv/bin/python ../.venv/bin/maturin develop --release`
- `.venv/bin/pytest tests/domain/test_native_search.py` passed: 6 passed.
- `.venv/bin/python scripts/bench_native_search.py --rows 10000 --dims 384 --rounds 2`: release native-list 12.86x, native-numpy 139.25x over pure Python on this host.

Artifacts:
- Work log: `/tmp/rust-vector/codex-out.log`
