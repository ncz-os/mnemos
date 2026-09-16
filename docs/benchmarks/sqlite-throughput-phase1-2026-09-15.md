# MNEMOS SQLite Persistence Throughput — Phase 1

Refreshed 2026-09-16 from clean source `0ea31e2ec9a0d3418af764d245c31377f5fafdb0`. The canonical
artifact is `docs/proof/bench-sqlite-phase1-20260916T041833Z.{json,md}`.
The earlier 2026-09-15 artifacts remain available in git history.

## Scope

| Knob | Values |
|---|---|
| Corpus size | 1,000 / 10,000 memories |
| Concurrency | 1 / 5 / 10 writer tasks |
| Backend | sqlite, aiosqlite, one shared connection |
| Vectors | Deterministic mock float[768]; no inference |
| Warmup | 64 inserts per cell, excluded from insert timing |
| Read-back | Persisted count must equal corpus plus warmup |
| Platform | macOS arm64, Python 3.13.9 |
| Total matrix wall | 46.7821 seconds |

## Results

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 744.68 | 1.189 | 2.013 | 4.085 | 1.341 | 1.3429 | 1.565 |
| 1000 | 5 | 746.56 | 6.311 | 9.144 | 10.073 | 6.68 | 1.3395 | 1.5604 |
| 1000 | 10 | 742.16 | 13.153 | 16.826 | 19.391 | 13.405 | 1.3474 | 1.5417 |
| 10000 | 1 | 732.93 | 1.214 | 2.005 | 4.47 | 1.363 | 13.6439 | 14.2801 |
| 10000 | 5 | 752.23 | 6.107 | 9.605 | 11.434 | 6.644 | 13.2938 | 13.9301 |
| 10000 | 10 | 772.76 | 12.589 | 15.986 | 17.908 | 12.932 | 12.9406 | 13.5712 |

These are single-run measurements on an interactive development host, without
exclusive hardware allocation. They are not production capacity estimates or
controlled comparisons with the earlier Linux/Python 3.14 run.

Each cell reports `peak_in_flight_inserts`: observed values match requested
concurrency (1, 5, 10). Tasks overlap, then queue at `SqliteBackend.transactional`'s
shared connection lock and `BEGIN IMMEDIATE`. This measures the backend's current
serialization, not parallel SQLite writers. Increasing concurrency raises latency
without proportional throughput gains. It does not measure end-to-end API latency,
retrieval, mixed workloads, durability under failure, or real embeddings.

`insert_wall_seconds` times only the measured inserts. `wall_seconds` now includes
setup, deterministic record generation, warmup, read-back, close and cleanup. The
original artifact incorrectly excluded setup and cleanup from the latter field;
its insert throughput was unaffected. The harness now cancels workers and removes
temporary databases even when an insertion fails.

## Reproduction

Use a clean committed checkout with `.[dev,sqlite]` installed. Validation writes
temporary artifacts and removes them rather than adding new proof files:

```bash
python scripts/bench_sqlite_throughput_phase1.py --dry-run
```

To refresh the single canonical pair, run with `--output-dir` pointing to a staging
directory, then replace the existing `docs/proof/bench-sqlite-phase1-*` pair and
update this reference and `benchmarks/README.md`. Preserve source commit, platform,
and raw measurements; do not infer scale from this small matrix.

## Phase 2

Phase 2 is blocked on an operator hardware-allocation decision. No 100k/1M corpus,
concurrency 25/50/100, multi-process contention, or packing-density tests were run.
