# MNEMOS SQLite throughput bench — Phase 1

Run at: `2026-09-15T18:27:38.365512+00:00`  
Git SHA: `21af4def36e5f2221f22fb1394ad5e94eeb578a9`  
Python: `3.14.6 (main, Jun 11 2026, 00:00:00) [GCC 16.1.1 20260515 (Red Hat 16.1.1-2)]`  
Platform: `Linux-7.0.13-400.asahi.fc44.aarch64+16k-aarch64-with-glibc2.43`  
Backend: `sqlite` (mock vectors, embedding_dim=768)  
Schema version: `mnemos-sqlite-bench-phase1/v1`

## Results — corpus_size x concurrency -> throughput + p50/p95/p99

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 1648.53 | 0.48 | 0.957 | 1.253 | 0.606 | 0.6066 | 0.6133 |
| 1000 | 5 | 1146.35 | 4.412 | 6.512 | 7.212 | 4.355 | 0.8723 | 0.8792 |
| 1000 | 10 | 1327.9 | 8.185 | 12.688 | 14.035 | 7.467 | 0.7531 | 0.7755 |
| 10000 | 1 | 1465.92 | 0.56 | 1.121 | 1.469 | 0.681 | 6.8216 | 6.8873 |
| 10000 | 5 | 1259.31 | 3.173 | 7.806 | 10.011 | 3.968 | 7.9409 | 8.0488 |
| 10000 | 10 | 1102.52 | 6.957 | 19.881 | 23.867 | 9.062 | 9.0701 | 9.1587 |

## Architectural note

SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.

## Phase 2 (NOT done)

Phase 1 stops at corpus sizes 1k and 10k, concurrency levels 1, 5, and 10. Phase 2 would extend the corpus to 100k and 1M, the concurrency to 25, 50, and 100, and add multi-instance packing-density testing. Blocked on an operator hardware-allocation decision. The CLI flags on this script (`--corpus-sizes`, `--concurrency`) already accept larger values, so the natural extension point is to widen the matrix when Phase 2 is unblocked — no script rewrite needed.

