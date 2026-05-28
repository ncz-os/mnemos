# Persistence Protocol Split

## Delivered

- Split the monolithic persistence facade into runtime-checkable capability protocols:
  `CorePersistence`, `OAuthPersistence`, `SessionsPersistence`,
  `ConsultationsPersistence`, `FederationPersistence`, `AuditPersistence`,
  and `StatePersistence`.
- Kept `PersistenceBackend` as a backwards-compatible type alias over the
  capability protocols.
- Added `backend.capabilities` to concrete backends:
  - SQLite: all capabilities.
  - Postgres: all capabilities.
  - Oracle: core, federation, audit, state.
  - Db2: core only.
- Added API capability guards that return `BackendCapabilityMissing` HTTP 503
  before unsupported OAuth, sessions, consultations, federation, audit, or state
  paths can hit repository stubs.
- Added startup capability logging and `MNEMOS_REQUIRE_CAPABILITIES` fail-fast
  validation in lifecycle startup.
- Added focused protocol/capability tests under
  `tests/persistence/test_capability_protocols.py`.

## Verification

- `bash -n deploy.sh install.sh docker-gpu-setup.sh git_sync_daily.sh`
  - Result: passed
- `.venv/bin/python -m py_compile ...`
  - Result: passed for changed persistence, API, lifecycle, domain, worker, audit,
    and test files.
- `.venv/bin/pytest -q tests/persistence/`
  - Result: `4 passed`

## Commits

- `b8fe078` `refactor persistence capability protocols`

## Logs

- `/tmp/persistence-split/codex-out.log`

No migrations were added. No redeploy was performed.
