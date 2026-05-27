# KNEMON Adversarial Review Report

Date: 2026-05-27
Branch: feat/knemon-mvp

Scope reviewed by tree grep: usage_ledger, ledger, knemon, price, triage.

## Findings Fixed

1. Unknown model rows could be recorded with zero estimated cost.
   - File: `mnemos/persistence/postgres.py`
   - Problem: `record_usage_ledger` used a fallback `resolved_prices` CTE that produced zero prices when no `model_registry` row matched. That preserved server-side computation, but silently undercounted unregistered provider/model usage.
   - Fix: changed the `INSERT ... SELECT` to read directly from `model_registry WHERE provider=$1 AND model_id=$2`; if no row matches, `fetchrow` returns `None` and the method raises `RuntimeError`.
   - Verification: added `test_postgres_record_usage_ledger_fails_closed_for_unknown_model`.

2. 0032 migration schema drift across PostgreSQL, Oracle, and Db2.
   - Files:
     - `db/migrations/0032_usage_ledger.sql`
     - `db/migrations_oracle/0032_usage_ledger.sql`
     - `db/migrations_db2/0032_usage_ledger.sql`
   - Problem: PostgreSQL and Oracle allowed nullable `tokens_reasoning` and nullable `ts`, while Db2 required both. No dialect constrained `est_cost_usd >= 0`.
   - Fix: made `tokens_reasoning` and `ts` non-null where missing, and added non-negative estimated-cost checks across all three dialects.
   - Verification: added `test_usage_ledger_migrations_preserve_constraint_parity`.

## Findings Reviewed, No Code Change Needed

1. SQL injection / parameter binding.
   - `mnemos/persistence/postgres.py` ledger insert uses asyncpg placeholders `$1` through `$10`.
   - Provider sync SQL and arena update SQL use placeholders.
   - PANTHEON price/catalog queries are static SQL without user interpolation.

2. Client-trusted cost.
   - `/v1/ledger` request does not accept `est_cost_usd`.
   - Cost remains computed server-side from `model_registry`.

3. Silent ledger write failure.
   - `mnemos/llm.py` awaits `_record_usage` in `finally` and does not swallow recorder exceptions.
   - `/v1/ledger` only maps `NotImplementedError` to 503; other write failures are not silently converted to success.

4. `/v1/ledger` auth and validation.
   - Route depends on `get_current_user`.
   - Pydantic validates non-negative token and latency counts.
   - Outcome is restricted to `ok`, `err`, or `timeout`.

## Verification

Targeted tests:

```text
.venv/bin/python -m pytest tests/test_knemon_ledger.py
4 passed
```

Notes:

- `python -m pytest tests/test_knemon_ledger.py` could not run because `python` is not on PATH in this environment.
- `python3 -m pytest tests/test_knemon_ledger.py` could not run because the system interpreter lacks `pytest_asyncio`.
- `python3 scripts/check_migration_parity.py --mode full` reports pre-existing historical migration gaps unrelated to 0032. The staged migration parity hook should be run in staged mode after staging the 0032 files.
