Concurrency parity tests for hive job claims

Implemented:
- Added `tests/hive_mind/test_concurrency_parity.py`.
- The test pre-seeds 1000 queued jobs, races 8 async workers through an in-process `POST /v1/jobs/next?agent_urn=...` route, and asserts:
  - total claims are exactly 1000,
  - claimed job IDs are unique across workers,
  - every seeded job was claimed,
  - no foreign jobs were claimed,
  - no seeded jobs remain queued.
- Parameterized the parity arm across:
  - `SqliteHiveMindRepository` always,
  - `OracleHiveMindRepository` when `ORACLE_DSN` is set,
  - `Db2HiveMindRepository` skipped until the class and `DB2_DSN` exist.
- Added an Oracle claim-path guard that pins `FOR UPDATE SKIP LOCKED`, the `status='queued'` update predicate, and the P0-1 `rowcount != 1` correctness check.
- Added the SQLite job-queue surface needed for repository parity:
  - `memory_jobs` test schema in `init()`,
  - `insert_job` / `insert_job_queued`,
  - `claim_next_job` / `find_and_claim_job`,
  - `list_jobs`.

Verification:
- `.venv/bin/pytest tests/hive_mind/test_concurrency_parity.py` passed: 2 passed, 2 skipped.
- `.venv/bin/pytest tests/hive_mind/test_oracle_repository_complete.py tests/hive_mind/test_concurrency_parity.py` passed: 3 passed, 2 skipped.
- `.venv/bin/python -m py_compile mnemos/hive_mind/repository.py tests/hive_mind/test_concurrency_parity.py` passed.
- Plain system `pytest` could not load the repo test suite because `/opt/homebrew/bin/python3` lacks `pytest_asyncio`; the repo `.venv` test runner was used.
- `ruff` was not installed in `.venv`.

Live backend note:
- `ORACLE_DSN` and `DB2_DSN` were not present in this shell, so the live Oracle and Db2 arms skipped locally.
- The test is wired to run Oracle automatically when `ORACLE_DSN` is supplied; no migrations or redeploy were performed.

Artifacts:
- Pytest output: `/tmp/concurrency-parity/codex-out.log`
