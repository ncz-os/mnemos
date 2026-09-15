# Live backend regression validation

The federation journal and MORPHEUS paths are exercised through real persistence
backends in `tests/test_federation_journal.py` and
`tests/test_morpheus_backend_live.py`. These are repository/driver integration
tests, not a production capacity or end-to-end inference benchmark.

Run with Python 3.13 and the relevant optional database drivers installed:

```sh
MNEMOS_EMBEDDING_DIM=3 pytest tests/test_federation_journal.py tests/test_morpheus_backend_live.py -q
```

PostgreSQL uses `MNEMOS_TEST_DB`; MySQL and MariaDB use
`MNEMOS_TEST_MYSQL_DSN` and `MNEMOS_TEST_MARIADB_DSN`. These fixtures create
and drop randomly named disposable databases. Oracle and Db2 require dedicated
test schemas/databases via `MNEMOS_TEST_ORACLE_DSN` and `MNEMOS_TEST_DB2_DSN`,
plus `MNEMOS_TEST_ALLOW_RESET=1`: their fixture data is deleted between cases.
Never point these variables at an operational database. Db2's database must be
created with 32 KiB pages; the schema's later ALTER statements exceed 4 KiB rows.
The test vectors use three dimensions; provision a fresh test schema accordingly.

The September 15 review ran against PostgreSQL 17/pgvector, SQLite/sqlite-vec,
MySQL 9.1, MariaDB 12.3.3, Oracle 26ai (23.26.1), and Db2 12.1.5.
Coverage includes committed journal ordering, hard/soft deletions, scope and
provenance changes, stale replay, re-sharing, vector serialization, sparse
checkpoints, owner-separated consolidation, phase rollback, synthesis, extraction,
and full-content deletion hashes. Test results must always cite the tested commit.

## Db2 SQL boundary

Native Db2 repositories keep strict native cursors. MORPHEUS still inherits
Oracle SQL and now has an explicit `Db2MorpheusNativeBridge` using the existing
SQL adapter on the same physical transaction. It neither acquires another
connection nor commits independently. Native-mode live tests include transaction
rollback and the complete repository phase flow. This fixes the previously
broken path; it does not claim the inherited MORPHEUS implementation is native
Db2 SQL. Removing that compatibility layer remains a separate refactor.

## Migration and operating boundaries

0064 repairs already-installed journal triggers without resetting events or
cursors. Quiesce MySQL-family writers during trigger replacement: their DDL is
not transactional. Pre-journal hard deletions cannot be reconstructed.
Required audit coverage remains limited to the documented mutation paths.
Scale, hardware packing density, live provider quality, and native accelerator
performance require separate workload-qualified measurements.
