# KNEMON Date-Aware Plan Transitions

## Delivered

- Added Oracle migration `0035_subscription_plans_date_aware.sql` with guarded `subscription_plans` date/path columns, `usage_ledger.path_kind`, Anthropic Max transition rows, Agent SDK credit-pool row, and xAI SuperGrok.
- Added PostgreSQL and Db2 mirror migrations for schema parity.
- Updated KNEMON routing and utilization queries to use active plans only with `effective_from <= TRUNC(SYSTIMESTAMP)` and open-ended/active `effective_until`.
- Threaded `path_kind` through ledger API payloads and Oracle/Postgres/Db2/SQLite ledger inserts.
- Added `path_kind` to utilization/session/cost split breakdowns.
- Kept fresh-install migration lists aligned in installer and docker-compose files.

## Production

- Applied `db/migrations_oracle/0035_subscription_plans_date_aware.sql` to PYTHIA Oracle `ORCLPDB1` as `mnemos`.
- Verified expected Anthropic/xAI rows and `usage_ledger.path_kind` on PYTHIA.
- No redeploy performed.

## Verification

- `bash -n scripts/oracle_add_nomic_col.sh scripts/oracle_swap_to_bge_m3.sh deploy/zeroclaw-fanout/deploy_fleet.sh`
- `.venv/bin/python -m py_compile ...`
- `.venv/bin/python -m pytest tests/test_knemon_ledger.py tests/domain/test_knemon_router.py tests/api/test_knemon_utilization.py tests/test_migration_lists_sync.py`
- Logs: `/tmp/knemon-date-aware/codex-out.log`
