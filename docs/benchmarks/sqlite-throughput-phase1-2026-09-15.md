# MNEMOS SQLite Persistence Throughput — Phase 1, 2026-09-15

First measurement of MNEMOS v7's SQLite persistence insert path
(`mnemos.persistence.sqlite.SqliteBackend` →
`SqliteMemoryRepository.insert_memory`, line ~927 of
`mnemos/persistence/sqlite.py`) under a controlled concurrency matrix.
This is the v7 roadmap Feature 5/5 Phase 1 deliverable — the actual
artifact is the committed JSON, not this document.

The matrix was run end-to-end against a fresh temp SQLite file per cell
(no shared or persistent DB, no real data path pollution), then the
resulting JSON + markdown summary were committed to
`docs/proof/bench-sqlite-phase1-20260915T175740Z.{json,md}`.

This document discharges the Phase 1 shipping criterion:

> Run the full 6-cell Phase 1 matrix and commit the resulting
> artifacts under `docs/proof/`. Phase 2 (100k / 1M corpus,
> concurrency 25 / 50 / 100, multi-instance) is explicitly out of
> scope and blocked on an operator hardware-allocation decision.

## Scope (Phase 1, done)

| Knob        | Values             |
|-------------|--------------------|
| Corpus size | 1,000 / 10,000 memories |
| Concurrency | 1 / 5 / 10 concurrent writer tasks |
| Backend     | SQLite (`SqliteBackend`) |
| Vectors     | MOCK — deterministic random float[768], seeded per cell. No inference endpoint touched. |
| Warmup      | 64 inserts per cell (excluded from timed measurement) |
| Read-back   | 1 `gather_stats()` call per cell (sanity, not headline) |
| Cells       | 2 × 3 = 6 |
| Total wall  | ~29 s on the dev host (Asahi aarch64, python 3.14.6) |
| DB path     | Fresh tempfile per cell, removed before the cell returns |

Total 6-cell runtime is well under the 30-minute budget; the script
has headroom for Phase 2 once the operator decision lands.

## Headline numbers (committed artifact: `bench-sqlite-phase1-20260915T175740Z.md`)

| corpus_size | concurrency | throughput (ops/s) | p50 (ms) | p95 (ms) | p99 (ms) | insert wall (s) |
|---:|---:|---:|---:|---:|---:|---:|
| 1000   | 1  | 1296.34 | 0.716 | 1.152 | 1.510 | 0.78 |
| 1000   | 5  | 1197.11 | 4.569 | 6.129 | 6.856 | 0.84 |
| 1000   | 10 | 1540.32 | 5.644 | 9.451 | 10.143 | 0.66 |
| 10000  | 1  | 1219.35 | 0.855 | 1.331 | 1.671 | 8.28 |
| 10000  | 5  | 1262.73 | 4.165 | 5.803 | 6.991 | 7.98 |
| 10000  | 10 | 1316.96 | 7.272 | 11.127 | 13.169 | 7.65 |

(Values are from one run on the dev host; the JSON artifact is the
source of truth. Throughput should be read as a single-host ballpark
on this Python + filesystem combo — it is not a hardware ceiling.)

## What Phase 1 measured

- Per-insert latency distribution (p50 / p95 / p99 / mean / min / max)
  for the canonical `SqliteMemoryRepository.insert_memory` code path
  with mock 768-dim vectors.
- Aggregate insert throughput (ops/sec) per (corpus_size, concurrency).
- A small read-back pass (`gather_stats()`) after the timed inserts to
  confirm the corpus was actually visible to the SQL layer; included
  for completeness, not as a headline metric.
- Wall-clock separation: `insert_wall_seconds` (timed region only) is
  reported separately from `wall_seconds` (the cell including
  `SqliteBackend.open()` + migrations + sqlite-vec load + close), so
  open-cost noise is visible in the JSON artifact even though it is
  amortized to nothing at 10k corpus.

## What Phase 1 surfaced (architectural observation, not a bug)

`SqliteBackend.transactional()` (line ~7401 of
`mnemos/persistence/sqlite.py`) acquires a single `asyncio.Lock` and
opens one connection that runs `BEGIN IMMEDIATE` per transaction.
That means **all writes serialize through one path**, regardless of
how many concurrent writer tasks the bench dispatches. The numbers
above show this clearly:

- Throughput plateaus at roughly 1.2-1.5k ops/s across concurrency
  levels 1, 5, and 10 — adding more tasks queues them at the lock,
  it does not parallelize SQLite writes.
- Per-insert latency scales up with concurrency (p50 ~0.85 ms at c=1
  vs ~7.3 ms at c=10 on the 10k corpus) — the extra tasks are
  paying for time spent waiting on the lock, not for additional
  work done in parallel.

This is the actual behavior of the SQLite backend in MNEMOS today.
It is **not a benchmark artifact** and it is **not a bug introduced
by the bench**. Phase 1's job was to measure the system as-is; Phase
2 will revisit this in a different context (multi-process packing).

## What Phase 1 explicitly did NOT do (and why)

The v7 roadmap blocks Phase 2 on an operator hardware-allocation
decision that has not been made. To avoid speculative work, this
bench stops at the Phase 1 matrix:

- **No 100k or 1M corpus cells.** The 10k cell already establishes
  the per-insert cost; the linear-extrapolation case for larger
  corpora is left for Phase 2 where the operator has allocated
  hardware for a heavier run.
- **No concurrency 25 / 50 / 100.** Adding more tasks past 10 only
  makes the lock-queueing effect noisier, not more informative, on
  a single-process backend.
- **No multi-instance or packing-density testing.** That requires
  multiple `mnemos` containers pointing at the same SQLite file (or
  separate files merged later), which is the entire point of Phase 2
  and is blocked on the operator decision.
- **No real embeddings.** This bench is about persistence-layer
  throughput/latency, not embedding quality. Vectors are deterministic
  random float[768] — there is no inference endpoint in the call
  graph, no GPU involved, no flaky network in the measurement.
- **No semver-bumping docs or version-string changes.** The bench
  is the Phase 1 deliverable; `pyproject.toml` /
  `mnemos/_version.py` are untouched (the version-pin test gate is
  run as a guardrail, not as a deliverable update).

## Reproducing this benchmark

The bench script is committed at
`scripts/bench_sqlite_throughput_phase1.py`. The full Phase 1 matrix
is one command:

```bash
python scripts/bench_sqlite_throughput_phase1.py \
    --corpus-sizes 1000,10000 \
    --concurrency 1,5,10 \
    --output-dir docs/proof
```

The script opens a fresh `tempfile.mkdtemp()`-backed SQLite file per
cell and removes it before returning, so it can be run repeatedly
on a host without polluting any real data path. The JSON artifact
is provenance-tagged with git SHA, Python version, platform, and
UTC run timestamp.

Required environment:

- Python 3.11+ (the bench uses `match`-style type hints, `asyncio.Queue`,
  and `tempfile.mkdtemp`).
- `mnemos` importable on `sys.path`. The bench inserts the repo root
  onto `sys.path` itself (`scripts/bench_sqlite_throughput_phase1.py`
  line ~46), so a bare `python scripts/...` from the repo root works
  without an editable install — matching the convention used by the
  other bench scripts in this repo.

Expected timing on the dev host (Asahi aarch64, python 3.14.6):
~29 seconds for the full 6-cell matrix. Will be slower on
architectures with weaker single-thread SQLite performance; faster
on NVMe-backed desktop builds. None of these changes the
architectural observation in the section above — the lock-queueing
shape is hardware-independent.

## Phase 2 — what gets added when the operator unblocks it

Phase 2's matrix widens:

| Knob        | Phase 1    | Phase 2 (when unblocked) |
|-------------|------------|-------------------------|
| Corpus size | 1k, 10k    | 100k, 1M                |
| Concurrency | 1, 5, 10   | 25, 50, 100             |
| Layout      | single-process `SqliteBackend` | multi-instance / packing-density |

The bench script's CLI already accepts larger values via
`--corpus-sizes` and `--concurrency` (comma-lists). When Phase 2 is
unblocked the change is matrix values + a separate packing-density
section, not a script rewrite.

## Gate results for this commit

- `ruff check .` — clean.
- `python -m pytest tests/test_doc_version_pins_match_code.py -q` —
  19/19 passed (version-pin guardrail; this task doesn't touch
  version strings but the gate is required).
- The check command from the task brief:

  ```bash
  python scripts/bench_sqlite_throughput_phase1.py \
      --corpus-sizes 1000,10000 --concurrency 1,5,10 \
      --output-dir docs/proof \
      && ruff check . \
      && python -m pytest tests/test_doc_version_pins_match_code.py -q
  ```

  exits 0 end-to-end.