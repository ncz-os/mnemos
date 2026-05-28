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
