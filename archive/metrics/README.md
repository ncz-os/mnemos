# Archive: Prometheus Metrics

**Source**: `mnemos.git` (Feb 2026 snapshot)
**Status**: NOT WIRED — production has zero observability instrumentation
**Integration effort**: LOW

## What it does

`metrics.py` — 358 lines. Full Prometheus/prometheus_client integration:
- Request counters and latency histograms (by endpoint, method, status code)
- Cache hit/miss rates (both memory and embedding cache)
- DB query latency and connection pool utilization
- GRAEAE consultation latency by provider
- Flask Blueprint exposing `/metrics` in Prometheus text format

## How to integrate

1. `pip install prometheus_client` (add to requirements.txt)
2. Copy `metrics.py` → `/opt/mnemos/api/handlers/metrics.py`
3. Replace Flask Blueprint with FastAPI router (minor adaptation)
4. In `api_server.py`: `from api.handlers.metrics import router as metrics_router` and `app.include_router(metrics_router)`
5. Instrument call sites in `memories.py` and `graeae_routes.py` with the counter/histogram helpers

## Notes

- No secret values hardcoded; safe to use as-is after Flask→FastAPI conversion
- Requires prometheus_client package (not currently in requirements.txt)
