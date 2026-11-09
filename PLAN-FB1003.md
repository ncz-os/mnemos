# PLAN-FB1003 — mnemos: sqlite branch repository (tests only)

Brief: FREEBUFF-LANE-BRIEF-67.md `### FB1003.` (base `master`).

## Scope
- New file: `tests/test_sqlite_branch_repo_20261107.py`.
- Production untouched. Symbols: `mnemos.persistence.sqlite.SqliteBranchRepository`,
  its mixin `_SqliteRepository`, and the base `mnemos.persistence.base.BranchRepository`.

## Established facts
- `SqliteBranchRepository(_SqliteRepository, BranchRepository)`, `__init__` takes no
  arguments, `__abstractmethods__` empty, four own methods plus three private helpers.
- `_SqliteRepository._conn(tx)` refuses anything that is not a `SqliteTransaction`
  (`TypeError`) — the dialect guard.
- SQLite schema (`mnemos/db_migrations/migrations_sqlite/migrations.sql`): `memory_branches(memory_id, name,
  head_version_id, created_by, created_at)` with `PRIMARY KEY (memory_id, name)` and FKs
  to `memories(id)` / `memory_versions(id)`; the backend opens with `foreign_keys=ON`, so a
  roundtrip needs a real memory row first.
- `SqliteBackend.open()` applies migrations; `transactional()` yields a `SqliteTransaction`.

## Approach
Contract assertions (bases, MRO, concreteness, method set, parameter parity, dialect guard,
no attribute fallback) plus a real in-file SQLite roundtrip against `tmp_path`: open the
backend, create one memory through the memory repository, insert a branch row, delete it
through the repository under test, and confirm the row is gone. The documented empty-list
short-circuits are covered with `[]`.

## Cases
1. Exact bases `(_SqliteRepository, BranchRepository)` and MRO containing both.
2. Concrete: `inspect.isabstract` False, `__abstractmethods__` empty.
3. Argument-free construction is inert (empty `__dict__`, no path/connection argument).
4. All four documented methods defined on the class, concrete, coroutines.
5. Parameter parity (name, kind, default) with `BranchRepository`.
6. `_conn` refuses a non-`SqliteTransaction` with `TypeError`.
7. Unknown attributes raise `AttributeError`; no `__getattr__`.
8. Schema acceptance: `memory_branches` exists with the documented columns and the
   `(memory_id, name)` composite primary key.
9. Roundtrip: two branch rows are inserted in a transaction; the repository deletes only
   the requested `memory_id`'s row, the other memory's branch row is untouched, and both
   `memories` rows survive (so the deletion cannot be a cascade or table-wide cleanup).
10. `fetch_memory_branch_heads(tx, [])` returns `[]` and
    `delete_memory_branches_for_memories(tx, [])` returns `None`.

## Risks / divergences
- The repo class adds three private helpers beyond the ABC surface; contract tests assert
  the public four only, and the extra surface is recorded.
- `fetch_memory_branch_heads` reads `memory_versions`, not `memory_branches`, so a branch
  row with no version rows is invisible to it — checked only through the empty path here.

## Verification
`pytest tests/test_sqlite_branch_repo_20261107.py -q` plus `ruff check`.
