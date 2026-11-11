# PLAN FB979 — memory column projection (tests only)

Target: `mnemos/persistence/sqlite.py::_sqlite_memory_cols`.
Deliverable: `tests/test_memory_cols_20261107.py` plus this plan.

## What gets pinned

- The projection is a deterministic string with exactly one entry per
  `base.MEMORY_COLS` name, in the same order, and `cursor.description` reports
  those same names — so no column is dropped, duplicated or renamed.
- `compressed_content` is emitted as `NULL AS compressed_content` (and never as a
  column reference); `created` and `updated` are emitted as
  `replace(datetime(<col>), ' ', 'T') AS <col>`; every other column passes
  through untouched.
- An alias prefixes every passthrough column and the `datetime()` argument, but
  not the `NULL AS compressed_content` literal and not the `AS` names; the empty
  alias renders exactly the unaliased string.
- The list is valid SQL: it executes as a `SELECT` list against an in-memory
  `memories` table, with and without an alias, and a stored timestamp renders in
  ISO shape while `compressed_content` comes back `None`.

## Divergences

- `DIVERGENCE:` `compressed_content` is hard-coded to `NULL`, so a SELECT built
  from this list can never return SQLite-stored compressed content — if the
  column is ever added, the projection silently keeps fabricating `NULL` rather
  than reading it.
- `DIVERGENCE:` the timestamp rewrite goes through `datetime()` and string
  replacement, which drops fractional seconds and the UTC offset (normalising an
  offset timestamp to UTC), so the wire shape is not identical to Postgres
  `datetime.isoformat()` as the docstring claims; a stored value SQLite cannot
  parse renders `NULL` rather than the original text, i.e. the rewrite is an
  unconditional filter over whatever is in the column.

## Files

- `PLAN-FB979.md` (new)
- `tests/test_memory_cols_20261107.py` (new)
