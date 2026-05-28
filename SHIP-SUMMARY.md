Oracle OAuth/Sessions/Consultations persistence

Implemented:
- Added Oracle facade methods for OAuth tokens/state, protocol sessions/logs, and consultations/responses.
- Oracle backend now advertises all seven persistence capabilities: core, oauth, sessions, consultations, federation, audit, state.
- Added SQLite parity facade methods and a SQLite 0038 test migration.
- Added 0038 migrations for Oracle, PostgreSQL, and Db2.
- Registered PostgreSQL 0038 in installer and docker-compose migration lists.
- Added `tests/persistence/test_oracle_oauth_sessions_consultations.py`.

Verification:
- `python3 -m py_compile mnemos/persistence/oracle.py mnemos/persistence/sqlite.py` passed.
- `.venv/bin/python -m pytest tests/persistence/test_oracle_oauth_sessions_consultations.py tests/persistence/test_capability_protocols.py tests/test_migration_lists_sync.py -q` passed: 16 tests.
- `ruff` was not installed in `.venv`, so no ruff pass was available.

Live Oracle:
- Initial default DSN `192.168.207.25:1521/FREEPDB1` failed: service not registered.
- Applied `db/migrations_oracle/0038_oauth_sessions_consultations.sql` to PYTHIA Oracle `192.168.207.67:1521/ORCLPDB1` as `mnemos`.
- Live Oracle protocol roundtrip passed for token/state/session/log/consultation/response, then cleaned up synthetic rows.

Smoke:
- Local Docker socket unavailable.
- `root@argonas` did not resolve locally; `origin` resolves to `root@192.168.207.101`.
- `root@192.168.207.101` Docker daemon was not reachable.
- PYTHIA `docker logs mnemos-api | grep -i capabilit` still shows pre-redeploy capabilities: `audit, core, federation, state`.
- No `mnemos-api` redeploy performed, per operator gate.
