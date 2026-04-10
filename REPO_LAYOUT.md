# Repository Layout

## Top-level structure

| Path | Purpose |
|------|---------|
| `api/` | FastAPI route handlers, auth middleware, request/response models |
| `api/handlers/` | One module per resource group (memories, kg, graeae, admin, etc.) |
| `db/` | Database schema, migrations, and connection utilities |
| `modules/` | Core subsystems: compression, memory categorization, routing, hooks, bundles |
| `graeae/` | GRAEAE consensus reasoning engine integration |
| `integrations/` | Optional third-party integrations (external LLMs, macrodata hooks) |
| `tests/` | pytest unit/integration tests; live E2E tests excluded from default run |
| `tools/` | Maintenance and migration utilities |
| `docs/` | Extended documentation |

## Key files

| File | Purpose |
|------|---------|
| `api_server.py` | FastAPI app entry point; mounts all routers |
| `core.py` | Shared database pool, embedding client, utilities |
| `config.py` | Configuration loader (reads `config.toml` and env vars) |
| `api_keys.py` | LLM provider API key loader |
| `install.py` | Interactive installer -- sets up Postgres, creates tables, configures `.env` |
| `distillation_worker.py` | Background worker: compresses old memories into summaries |
| `phi_server.py` | Optional local embedding server (Intel OpenVINO) |
| `mcp_server.py` | Model Context Protocol server for Claude integration |

## Configuration

Runtime configuration is controlled by `config.toml` (or env vars). See `DEPLOYMENT_GUIDE.md` for details.

## Adding a new API endpoint

1. Create a handler in `api/handlers/your_resource.py`
2. Import and mount the router in `api_server.py`
3. Add request/response models to `api/models.py`
4. Add auth: `user: UserContext = Depends(get_current_user)` on any protected route

## Running tests

```bash
pytest tests/          # unit + integration (no live infra needed)
pytest tests/test_live_e2e.py  # requires running MNEMOS instance
```
