# MNEMOS SQLite throughput bench — Phase 1

Run at: `2026-09-15T17:58:08.671888+00:00`  
Git SHA: `e8f0757a846b6a28e238830c014a2038b0513af9`  
Python: `3.14.6 (main, Jun 11 2026, 00:00:00) [GCC 16.1.1 20260515 (Red Hat 16.1.1-2)]`  
Platform: `Linux-7.0.13-400.asahi.fc44.aarch64+16k-aarch64-with-glibc2.43`  
Backend: `sqlite` (mock vectors, embedding_dim=768)  
Schema version: `mnemos-sqlite-bench-phase1/v1`

## Results — corpus_size x concurrency -> throughput + p50/p95/p99

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | mean (ms) | insert wall (s) | total wall (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000 | 1 | 1296.34 | 0.716 | 1.152 | 1.51 | 0.77 | 0.7714 | 0.779 |
| 1000 | 5 | 1197.11 | 4.569 | 6.129 | 6.856 | 4.17 | 0.8353 | 0.8416 |
| 1000 | 10 | 1540.32 | 5.644 | 9.451 | 10.143 | 6.464 | 0.6492 | 0.6624 |
| 10000 | 1 | 1219.35 | 0.855 | 1.331 | 1.671 | 0.819 | 8.2011 | 8.2833 |
| 10000 | 5 | 1262.73 | 4.165 | 5.803 | 6.991 | 3.958 | 7.9194 | 7.9849 |
| 10000 | 10 | 1316.96 | 7.272 | 11.127 | 13.169 | 7.589 | 7.5932 | 7.6521 |

## Per-cell schema note (repeated for offline readers)

- **corpus=1000 concurrency=1** — SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.
- **corpus=1000 concurrency=5** — SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.
- **corpus=1000 concurrency=10** — SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.
- **corpus=10000 concurrency=1** — SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.
- **corpus=10000 concurrency=5** — SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.
- **corpus=10000 concurrency=10** — SqliteBackend.transactional() acquires a single asyncio.Lock + BEGIN IMMEDIATE connection, so all writes serialize through one path. Concurrency > 1 measures how many tasks queue at the lock, not parallel SQLite writes.

## Phase 2 (NOT done)

Phase 1 stops at corpus sizes {1k, 10k} x concurrency {1, 5, 10}. Phase 2 would extend to 100k / 1M corpus and concurrency 25 / 50 / 100, plus multi-instance packing-density testing. Blocked on an operator hardware-allocation decision. The CLI flags on this script (`--corpus-sizes`, `--concurrency`) accept larger values, so the natural extension point is to widen the matrix when Phase 2 is unblocked — no script rewrite needed.

