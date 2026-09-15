# MNEMOS SQLite throughput bench — Phase 1

Run at: `2026-09-15T18:05:10.759447+00:00`  
Git SHA: `ebd917793ee8c8df0a1dda592b4a7704036580dd`  
Python: `3.14.6 (main, Jun 11 2026, 00:00:00) [GCC 16.1.1 20260515 (Red Hat 16.1.1-2)]`  
Platform: `Linux-7.0.13-400.asahi.fc44.aarch64+16k-aarch64-with-glibc2.43`  
Backend: `sqlite` (mock vectors, embedding_dim=768)  
Schema version: `mnemos-sqlite-bench-phase1/v1`

## Results — corpus_size x concurrency -> throughput + p50/p95/p99

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 1407.48 | 0.604 | 1.089 | 1.402 | 0.71 | 0.7105 | 0.7294 |
| 1000 | 5 | 1202.03 | 4.467 | 5.919 | 6.677 | 4.153 | 0.8319 | 0.8389 |
| 1000 | 10 | 1307.57 | 7.317 | 10.719 | 11.898 | 7.623 | 0.7648 | 0.7712 |
| 10000 | 1 | 1393.4 | 0.608 | 1.188 | 1.503 | 0.717 | 7.1767 | 7.2549 |
| 10000 | 5 | 1259.26 | 4.342 | 5.798 | 6.794 | 3.969 | 7.9412 | 8.0195 |
| 10000 | 10 | 1267.71 | 8.712 | 11.073 | 13.625 | 7.882 | 7.8882 | 7.9718 |

## Architectural note

SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.

## Phase 2 (NOT done)

Phase 1 stops at corpus sizes 1k and 10k, concurrency levels 1, 5, and 10. Phase 2 would extend the corpus to 100k and 1M, the concurrency to 25, 50, and 100, and add multi-instance packing-density testing. Blocked on an operator hardware-allocation decision. The CLI flags on this script (`--corpus-sizes`, `--concurrency`) already accept larger values, so the natural extension point is to widen the matrix when Phase 2 is unblocked — no script rewrite needed.

