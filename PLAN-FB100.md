# PLAN FB100 — mnemos federation dedup / archive interplay (F1, F2)

Base: master `0873b0e1`. Branch `freebuff/FB100-20261102`.

## F1 — already fixed at head (premise false)
Every backend's `find_duplicate_content_groups` AND `consolidate_duplicate_memories` already AND
`_eligibility.local_sweep_predicate("")` (sqlite.py:2017/2074, postgres.py:1456/1498,
mysql.py:2848/2900, oracle.py:2329/2376, db2.py:2010/2063), and the switch
(`MNEMOS_FEDERATION_REPLICA_FENCE`) defaults ON (core/config.py:2047-2070). Covered by
`tests/test_federation_replica_fence.py::test_artemis_dedup_groups_exclude_replicas`.
So: no production change; record it and keep the existing test as the proof.

## F2 — three of four sites already fenced; one and a half left
Already fenced: `worker_lifecycle.sweep_for_archival` (`_replica_fence()`, F-3 task) and the
repair path `restore_federated_memory_after_local_damage` (sets `archived_at = NULL`).

Still broken at head:
1. `domain/persephone/runner.py::_ELIGIBLE_SQL` — the asyncpg PERSEPHONE sweep, also used by
   `domain/admin_lifecycle_repo.sweep_for_archival`'s asyncpg path. No replica fence.
2. `domain/admin_lifecycle_repo.sweep_for_archival` portable branch — its own `base` SELECT with
   no replica fence.
3. `persistence/federation_journal.py::prepare_versioned_update` — clears `deleted_at` and
   `consolidated_*` but not `archived_at`, so a source update writes fresh content onto a row
   that stays hidden as an archive stub, and a later restore would put the stale payload back.

## Change
- runner: `_ELIGIBLE_SQL` becomes `_ELIGIBLE_SQL_TEMPLATE` + `eligible_sql()`, which injects
  `local_sweep_predicate("")` (empty when the switch is off, exactly like the backend sweeps).
  Call sites updated in runner (1) and admin_lifecycle_repo (1).
- admin_lifecycle_repo portable branch: same fence in its `base`.
- federation_journal.prepare_versioned_update: also `archived_at = NULL`.

## Tests — `tests/test_federation_replica_archive_fence.py`
1. `eligible_sql()` carries `federation_source IS NULL` with the switch on and drops it with the
   switch off (shape, no DB).
2. The asyncpg PERSEPHONE sweep sends the fenced statement (fake pool records `fetch` SQL).
3. Real SQLite: the admin lifecycle sweep archives a cold local row and leaves a cold replica
   alone (red at head).
4. Real SQLite: `prepare_versioned_update` clears `archived_at`, and the following versioned
   update leaves the replica live with the source's content (red at head).

## Risks
- `_ELIGIBLE_SQL` is private and only those two modules import it; renaming is checked by grep.
- FB98-style ORA-02014 shape in admin_lifecycle_repo's portable `base` is visible in the same
  function but belongs to no finding here: record as follow-up, do not fix in this branch.
