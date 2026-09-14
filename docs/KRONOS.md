# KRONOS

KRONOS is the MNEMOS time-series layer for recall-pattern anomaly detection and
recall-load forecasting. It reads the existing recall tracking surface on
`memories`: `recall_count` and `last_recalled_at`.

## Install

```bash
pip install mnemos-core[kronos]       # NumPy compute path
pip install mnemos-core[kronos-gpu]   # adds CuPy for the GPU backend
```

`kronos` is also a member of the `ml` bundle. The GPU backend is an independent
extra — install it only on a host with a working CUDA CuPy build.

## Runtime Gate

KRONOS is disabled by default.

```bash
MNEMOS_KRONOS_ENABLED=true
MNEMOS_KRONOS_SENSITIVITY=2.5
MNEMOS_KRONOS_LOOKBACK_HOURS=168
MNEMOS_KRONOS_BASELINE_DAYS=30
MNEMOS_KRONOS_BACKEND=auto        # auto | cpu | gpu
```

## Compute backends

`MNEMOS_KRONOS_BACKEND` selects the compute backend, resolved per call by
`mnemos/domain/kronos/backends/selector.py`:

| Value | Behaviour |
|---|---|
| `auto` (default) | Use the CuPy backend when `cupy` imports; otherwise NumPy. |
| `cpu` | Always NumPy (`backends/cpu.py`). |
| `gpu` | Request CuPy (`backends/gpu.py`). If CuPy is missing, that module logs a warning and re-exports the NumPy implementation, so the call still succeeds on CPU. |

Any other value raises `ValueError`.

The NumPy path is the correctness oracle. The CuPy EWMA mirrors it and matches
the input array type — NumPy in, NumPy out; CuPy in, CuPy out — so results are
interchangeable and a host can be moved between backends without a change in
observable output. Both implementations validate `0 < alpha <= 1` and return an
empty float array for empty input.

## Admin API

- `GET /admin/kronos/anomalies?namespace=<ns>` detects per-memory recall spikes
  and drops.
- `GET /admin/kronos/drift?namespace=<ns>` compares the last 7 days with the
  prior baseline window at namespace level.
- `GET /admin/kronos/forecast?namespace=<ns>&hours_ahead=24` forecasts recall
  load with an EWMA over hourly buckets and returns a 95% confidence interval.

All routes are root-only, and all three return `503` under any of three
distinct conditions:

1. **Extra not installed** — the `kronos` extra fails its probe. The response
   detail carries the install hint.
2. **Feature gate off** — `MNEMOS_KRONOS_ENABLED` is not true. Detail:
   `KRONOS disabled in this profile`.
3. **Non-PostgreSQL backend** — the routes go through
   `require_postgres_pool_or_503`. KRONOS queries are Postgres-only by design,
   so on a SQLite or edge profile the 503 says the endpoint requires a Postgres
   backend rather than reporting a phantom outage. The same guard returns 503
   with a transient-sounding detail when the backend *is* Postgres but the pool
   is not up (startup race or terminated pool).

## MCP Tools

- `kronos_anomalies(namespace)`
- `kronos_forecast(namespace, hours_ahead=24)`

Both are filtered out of the advertised MCP tool registry when the `kronos`
extra is not installed, so a client never sees a tool it cannot call. The
handler symbols still exist as stubs returning `{"success": false, "error":
"KRONOS not installed"}` rather than raising on import.

The MCP surface is read-only and uses the read rate-limit tier. Root callers may
inspect any namespace. Non-root callers only receive data for their own
namespace; cross-namespace anomaly requests return an empty result instead of a
distinguishable authorization error.
