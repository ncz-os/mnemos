# MNEMOS SQLite throughput bench — Phase 1

Run at: `2026-09-16T04:19:20.341242+00:00`  
Git SHA: `0ea31e2ec9a0d3418af764d245c31377f5fafdb0`  
Python: `3.13.9 (main, Nov 19 2025, 23:39:32) [Clang 21.1.4 ]`  
Platform: `macOS-27.0-arm64-arm-64bit-Mach-O`  
Backend: `sqlite` (mock vectors, embedding_dim=768)  
Schema version: `mnemos-sqlite-bench-phase1/v1`

## Results — corpus_size x concurrency -> throughput + p50/p95/p99

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 744.68 | 1.189 | 2.013 | 4.085 | 1.341 | 1.3429 | 1.565 |
| 1000 | 5 | 746.56 | 6.311 | 9.144 | 10.073 | 6.68 | 1.3395 | 1.5604 |
| 1000 | 10 | 742.16 | 13.153 | 16.826 | 19.391 | 13.405 | 1.3474 | 1.5417 |
| 10000 | 1 | 732.93 | 1.214 | 2.005 | 4.47 | 1.363 | 13.6439 | 14.2801 |
| 10000 | 5 | 752.23 | 6.107 | 9.605 | 11.434 | 6.644 | 13.2938 | 13.9301 |
| 10000 | 10 | 772.76 | 12.589 | 15.986 | 17.908 | 12.932 | 12.9406 | 13.5712 |

## Architectural note

SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.

## Phase 2 (NOT done)

Phase 1 stops at corpus sizes 1k and 10k, concurrency levels 1, 5, and 10. Phase 2 would extend the corpus to 100k and 1M, the concurrency to 25, 50, and 100, and add multi-instance packing-density testing. Blocked on an operator hardware-allocation decision. The CLI flags on this script (`--corpus-sizes`, `--concurrency`) already accept larger values, so the natural extension point is to widen the matrix when Phase 2 is unblocked — no script rewrite needed.

