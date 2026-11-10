# PLAN-FB1006 — mnemos: sqlite compression queue repository (tests only)

Brief: FREEBUFF-LANE-BRIEF-67.md `### FB1006.` (base `master`).

## Scope
- New file: `tests/test_sqlite_compression_queue_20261107.py`.
- Production untouched. Symbols: `mnemos.persistence.sqlite.SqliteCompressionQueueRepository`,
  mixin `_SqliteRepository`, base `mnemos.persistence.base.CompressionQueueRepository`.

## Established facts
- Bases `(_SqliteRepository, CompressionQueueRepository)`; no `__init__`; concrete; the
  eight abstract methods are all implemented with identical parameter lists, plus the
  private helper `_count_compressed_variants`.
- `memory_compression_queue` DDL: `id` PK, `memory_id` FK, `owner_id`, `reason` (CHECK on
  four values), `status` (CHECK pending/running/done/failed), `priority`,
  `scoring_profile` (CHECK on four values), `attempts`, `enqueued_at`, `started_at`,
  `finished_at`, `error`.
- `enqueue_compression` skips a memory that already has a `pending` row and returns the
  ids it enqueued; `dequeue_compression` claims with `status = 'running'`, bumps
  `attempts`, and orders by `priority DESC, enqueued_at`.

## Approach
Contract assertions plus a migrated temporary database. Each memory is created through
`backend.memories` so the queue FK holds; the repository is constructed directly and
handed real `SqliteTransaction`s.

## Cases
1. Exact bases and MRO.
2. Concrete, no abstract methods left.
3. Argument-free inert construction.
4. All eight documented methods defined on the class, concrete, coroutines.
5. Parameter parity with the base for all eight.
6. `_conn` refuses a foreign transaction; a real one is accepted.
7. No `__getattr__` fallback; unknown attributes raise.
8. Schema: table exists with the documented columns, `id` PK, `memory_id` FK.
9. Enqueue then dequeue returns the same item exactly once, `attempts == 1`, and a second
   dequeue returns the documented empty list.
10. Ordering: a higher-priority item enqueued second is claimed first (`priority DESC`).
11. Re-enqueueing a memory with a pending row enqueues nothing and adds no row.
12. `mark_compression_done` takes the item out of the pending set.
13. `get_queue_stats` returns a dict of integer counts.

## Risks / divergences
- `DIVERGENCE:` `dequeue_compression` increments `attempts` at claim time, so an item
  abandoned mid-run already carries an attempt.
- `DIVERGENCE:` ordering uses `enqueued_at` (second resolution) as the tiebreaker after
  priority, so equal-priority items enqueued in the same second have no defined order.

## Verification
`pytest tests/test_sqlite_compression_queue_20261107.py -q` plus `ruff check`.
