# MNEMOS SQLite throughput bench — Phase 1

Run at: `2026-09-15T18:16:53.157347+00:00`  
Git SHA: `d45c4fa7d4dd0014fbb8544e3f49982a6356447b`  
Python: `3.14.6 (main, Jun 11 2026, 00:00:00) [GCC 16.1.1 20260515 (Red Hat 16.1.1-2)]`  
Platform: `Linux-7.0.13-400.asahi.fc44.aarch64+16k-aarch64-with-glibc2.43`  
Backend: `sqlite` (mock vectors, embedding_dim=768)  
Schema version: `mnemos-sqlite-bench-phase1/v1`

## Results — corpus_size x concurrency -> throughput + p50/p95/p99

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 1594.37 | 0.569 | 0.99 | 1.224 | 0.626 | 0.6272 | 0.6334 |
| 1000 | 5 | 774.41 | 6.278 | 11.57 | 14.473 | 6.449 | 1.2913 | 1.2987 |
| 1000 | 10 | 862.74 | 10.666 | 20.116 | 23.41 | 11.566 | 1.1591 | 1.1653 |
| 10000 | 1 | 1002.06 | 0.775 | 2.074 | 3.139 | 0.997 | 9.9795 | 10.0408 |
| 10000 | 5 | 930.68 | 4.131 | 10.715 | 13.652 | 5.369 | 10.7448 | 10.846 |
| 10000 | 10 | 931.99 | 9.286 | 19.869 | 23.407 | 10.724 | 10.7297 | 10.8125 |

## Architectural note

SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.

## Phase 2 (NOT done)

Phase 1 stops at corpus sizes 1k and 10k, concurrency levels 1, 5, and 10. Phase 2 would extend the corpus to 100k and 1M, the concurrency to 25, 50, and 100, and add multi-instance packing-density testing. Blocked on an operator hardware-allocation decision. The CLI flags on this script (`--corpus-sizes`, `--concurrency`) already accept larger values, so the natural extension point is to widen the matrix when Phase 2 is unblocked — no script rewrite needed.

