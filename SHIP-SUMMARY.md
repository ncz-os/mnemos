# KNEMON DeepSeek Direct Registry Cleanup

## Delivered

- Added `db/migrations_oracle/0037_deepseek_direct_provider_seed.sql` to delete `parity_postgres_%` residue and upsert `deepseek-direct` rows for `deepseek-v4-flash` and `deepseek-v4-pro`.
- Added PostgreSQL and Db2 mirror migrations.
- Added `deepseek-direct` to provider sync static seeds and `data/llm_provider_registry.json`.
- Taught KNEMON routing to parse JSON-object capabilities from Oracle CLOB values.

## Production

- Applied Oracle 0037 to PYTHIA `ORCLPDB1` as `mnemos`.
- Verified `deepseek-direct = 2` rows and `parity_postgres_% = 0` rows in live `model_registry`.

## Verification

- `.venv/bin/python -m json.tool data/llm_provider_registry.json`
- `.venv/bin/python -m py_compile scripts/sync_provider_models.py mnemos/domain/graeae/provider_sync.py mnemos/domain/knemon/router.py`
- `.venv/bin/pytest tests/domain/test_knemon_router.py -q`
- Live smoke: `/v1/knemon/route?require_capability=reasoning` returned a valid route; after excluding subscription providers, current deployed code returned no matching model until the router JSON-object parser patch is deployed.
- Logs: `/tmp/knemon-deepseek-cleanup/codex-out.log`
