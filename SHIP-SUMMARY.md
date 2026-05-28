# KNEMON Task-Aware Recommender

## Delivered

- Added a shared Pantheon recommendation policy for `/v1/providers/recommend`.
- Mapped task types to explicit capability requirements:
  - `code-fix`, `code-generation`, `coding` require `coding`.
  - `narrative`, `chat`, `summarize`, `copywriting` require `chat` and exclude embeddings.
  - `reasoning` requires `reasoning`.
  - `embedding`, `embed` require a dedicated `embedding` model.
  - `routing`, `classification` require `routing` or small-context `chat`.
  - `web_search` requires `web_search`.
- Added per-task quality/cost policy and deterministic preferred-model fallbacks.
- Excluded special-purpose content-safety/moderation models from general recommendations.
- Reused the same recommender from Postgres MCP repo, SQLite persistence, and Db2 persistence.

## Verification

- `.venv/bin/pytest -q tests/domain/test_knemon_recommender.py tests/domain/test_knemon_router.py tests/test_persistence_parity.py::test_model_recommendation_lookup_and_available_models tests/test_db2_dialect_parity.py::test_db2_consultation_fetch_recommended_model_native`
  - Result: `22 passed in 0.40s`
- `python3 -m compileall -q mnemos/domain/pantheon/recommendation.py mnemos/db/mcp_repo.py mnemos/persistence/sqlite.py mnemos/persistence/db2.py mnemos/api/routes/providers.py`
  - Result: passed

## Live Smoke

- Target: `http://192.168.207.67:5002`
- `/health`: healthy, version `6.0.0rc1`
- `/v1/providers/recommend` for `code-fix`, `narrative`, `reasoning`, `embedding`, `routing`, and `web_search` still returned the deployed fallback `claude/claude-opus-4-6` with reason `model_registry empty; recommended highest-weight configured provider`.
- No redeploy was performed, per instruction. The live smoke confirms the currently deployed service has not picked up this branch's recommender patch yet.

## Logs

- `/tmp/knemon-recommender/codex-out.log`
