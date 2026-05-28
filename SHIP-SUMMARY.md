Oracle hive adapter completion

Implemented:
- `OracleHiveMindRepository.insert_message` writes to `hive_messages` with UUIDv7 RAW(16) ids and JSON payloads.
- `OracleHiveMindRepository.emit_event` writes to `hive_events`; it binds TIMESTAMP WITH TIME ZONE first and retries with epoch seconds only for live PYTHIA `ORA-00932` NUMBER compatibility.
- `OracleHiveMindRepository.cache_get` and `cache_store` read/write `hive_cache` using Oracle `MERGE`.
- `OracleHiveMindRepository.record_worker_kind_stats` upserts cumulative counters into `hive_worker_kind_stats`.
- Added a minimal `SqliteHiveMindRepository` parity helper for the completed hive methods.
- Added `tests/hive_mind/test_oracle_repository_complete.py`.

Verification:
- `bash -n scripts/*.sh` passed.
- `python3 -m py_compile mnemos/hive_mind/oracle_repository.py mnemos/hive_mind/repository.py tests/hive_mind/test_oracle_repository_complete.py` passed.
- `./.venv/bin/python -m py_compile mnemos/hive_mind/oracle_repository.py mnemos/hive_mind/repository.py tests/hive_mind/test_oracle_repository_complete.py` passed.
- `./.venv/bin/python -m pytest tests/hive_mind/ -q` passed: 1 test.

Smoke:
- `/opt/agent-bus-venv/bin/python` was not present on this host.
- Used repo `.venv` Python with `oracledb 4.0.1`.
- PYTHIA Oracle smoke succeeded against `192.168.207.67:1521/ORCLPDB1` as `mnemos`.
- Inserted synthetic message and emitted event: `message=019e6dba-d087-79cd-a230-b5210eaa6be6`, `topic=codex-smoke-29e3c32bd90c`.

Operational notes:
- No migration files changed.
- No `mnemos-api` or `graeae-hive` redeploy performed.
