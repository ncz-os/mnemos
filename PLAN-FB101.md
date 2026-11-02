# PLAN FB101 — mnemos tombstones, stats and replica updates (F3, P4, P5, S5, F4)

Base: master `0873b0e1`. Branch `freebuff/FB101-20261102`.

## Verified at head (line drift only)
- **F3** `oracle.py:7709` / `db2.py:3893` `apply_consolidation_tombstone` still soft-delete the loser
  (`SET deleted_at = SYSTIMESTAMP` / `CURRENT TIMESTAMP`) behind a comment claiming the schema "lacks
  memories.consolidated_into". `migrations_oracle/0045_consolidated_into.sql` and
  `migrations_db2/0045_consolidated_into.sql` both add it. Postgres/MySQL/SQLite set `consolidated_into`.
- **P4** `oracle.py:1535-1550`, `db2.py:1715-1730`: stats classify a replica by searching `metadata`
  for `federation_origin`; nothing writes that key (replicas carry `federation_source`), so
  `federated_memories` is always 0.
- **P5** `sqlite.py:6944` `apply_consolidation_tombstone`: neither the loser nor the canonical EXISTS
  requires `deleted_at IS NULL`, unlike Postgres (`postgres.py:6140`). Hides a live row behind a
  deleted canonical.
- **S5** `sqlite.py:2155-2195` `gather_stats`: no `deleted_at IS NULL` on any count, unlike every
  other backend.
- **F4** half false: the marker lookup no longer filters `deleted_at` on any backend, and the receiver
  deliberately counts + skips a locally soft-deleted replica (`federation.py`, prior F-2 task). What
  is still divergent is `delete_federated_memory`: Postgres/MySQL/Oracle/Db2 require
  `deleted_at IS NULL`, SQLite does not, so a source withdrawal leaves a ghost row on four backends.

## Change
- `oracle.py` / `db2.py`: tombstone sets `consolidated_into`, `consolidated_at`, `permission_mode = 400`
  with the same live-loser / live-canonical predicates as Postgres, and stops soft-deleting.
- `oracle.py` / `db2.py`: `gather_stats` counts `federation_source IS NULL` / `IS NOT NULL`.
- `sqlite.py`: tombstone adds `deleted_at IS NULL` on the loser and on the canonical EXISTS;
  `gather_stats` filters `deleted_at IS NULL` everywhere.
- `postgres.py` / `mysql.py` / `oracle.py` / `db2.py`: `delete_federated_memory` drops its
  `deleted_at IS NULL` predicate (match SQLite; a withdrawal owns the replica's whole lifecycle).

## Tests — `tests/test_federation_replica_lifecycle_parity.py`
Parity table first (per backend: tombstone target, stats key, delete predicate), then:
1. every backend's `delete_federated_memory` statement has no `deleted_at` predicate; SQLite
   additionally deletes a soft-deleted replica for real.
2. Oracle/Db2 tombstone SQL sets `consolidated_into` and assigns no `deleted_at`; MySQL keeps its
   `consolidated_into` tombstone.
3. Oracle/Db2 stats SQL keys on `federation_source` and never `federation_origin`.
4. Real SQLite: tombstone refuses a dead canonical and does not hide a live loser (red at head).
5. Real SQLite: stats exclude soft-deleted rows (red at head).

## Risks / limits
- No Oracle/Db2/MySQL server here: those are statement-shape tests through fake cursors.
- The side table `federation_consolidation_tombstones` keeps being written on Oracle/Db2 (other code
  reads it); only the memories mutation changes.
