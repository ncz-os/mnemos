# PLAN FB99 — mnemos `_Ops` LOB materialisation (finding P3)

Base: master (0873b0e1). Branch: `freebuff/FB99-20261102`.

## Verified at head
`_Ops.fetchone` (`worker_lifecycle.py:128`) and `_Ops.fetchall` (`:148`) build the
row dict straight from the driver tuple. The Oracle repositories instead route every
value through `oracle_helpers._materialize_value`, which awaits `read()` on LOBs.
The python-oracledb async driver returns CLOB columns as `AsyncLOB`, whose `repr()`
is a memory address, so the archive sweep serialises that address into the archive
and then overwrites `content` with `ARCHIVED:<id>` — the text is gone. The
hard-delete content hash at `:448` hashes the same repr. (The sweep only reaches
this on Oracle once FB98's claim fix lands, but the read path is wrong on its own.)

## Change
- `mnemos/persistence/worker_lifecycle.py`: one `_Ops._materialize(row)` helper used
  by `fetchone` and `fetchall`, delegating to the existing
  `oracle_helpers._materialize_value` for oracle/db2 only (the other drivers return
  plain str/bytes). No other behaviour change; Postgres rows stay untouched.

## Tests
New `tests/test_worker_lifecycle_ops_lobs.py` (fake LOB objects exposing `read()`,
no live database):
1. async-`read()` LOB (python-oracledb async) → `fetchone` returns the text and
   `_archive_payload(row)["memory"]["content"]` is that text, not a repr.
2. sync-`read()` LOB (sync driver) → same.
3. `fetchall` materialises every LOB in every row.
4. non-LOB values (`datetime`, `None`, plain str) pass through unchanged, and a
   Postgres row keeps the exact values asyncpg gave.

Red first: at head the payload holds `<FakeLOB object at 0x...>`.

## Risks
- No Oracle/Db2 driver here: the fakes model the documented `read()` shapes
  (async + sync) rather than a live connection. Say so in the summary.
- Materialising inside `_Ops` touches every lifecycle query, so the full
  life cycle/deletion/archive suites must stay green on SQLite and Postgres.
