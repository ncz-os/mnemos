# MNEMOS SQLite throughput bench — Phase 1

Run at: `2026-09-15T18:12:41.764455+00:00`  
Git SHA: `3aee7d59686daccc7789b732f9cf8b67e93e2302`  
Python: `3.14.6 (main, Jun 11 2026, 00:00:00) [GCC 16.1.1 20260515 (Red Hat 16.1.1-2)]`  
Platform: `Linux-7.0.13-400.asahi.fc44.aarch64+16k-aarch64-with-glibc2.43`  
Backend: `sqlite` (mock vectors, embedding_dim=768)  
Schema version: `mnemos-sqlite-bench-phase1/v1`

## Results — corpus_size x concurrency -> throughput + p50/p95/p99

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 941.4 | 0.822 | 2.199 | 3.359 | 1.061 | 1.0622 | 1.0687 |
| 1000 | 5 | 1703.01 | 2.705 | 3.928 | 5.833 | 2.926 | 0.5872 | 0.6094 |
| 1000 | 10 | 1195.07 | 6.9 | 19.228 | 21.11 | 8.342 | 0.8368 | 0.843 |
| 10000 | 1 | 1015.66 | 0.81 | 1.917 | 2.951 | 0.983 | 9.8458 | 9.9071 |
| 10000 | 5 | 1182.36 | 3.288 | 8.313 | 10.714 | 4.227 | 8.4577 | 8.5177 |
| 10000 | 10 | 1259.98 | 6.563 | 15.999 | 24.489 | 7.93 | 7.9366 | 8.02 |

## Architectural note

SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.

## Phase 2 (NOT done)

Phase 1 stops at corpus sizes 1k and 10k, concurrency levels 1, 5, and 10. Phase 2 would extend the corpus to 100k and 1M, the concurrency to 25, 50, and 100, and add multi-instance packing-density testing. Blocked on an operator hardware-allocation decision. The CLI flags on this script (`--corpus-sizes`, `--concurrency`) already accept larger values, so the natural extension point is to widen the matrix when Phase 2 is unblocked — no script rewrite needed.

