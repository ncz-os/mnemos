# Cross-backend parity audit (2026-06-13)

MNEMOS keeps a single persistence contract for PostgreSQL, Oracle, Db2,
MySQL, and SQLite. This note records the parity-sensitive paths audited for
this slice and the test coverage that guards them.

## Covered paths

- **Vault exclusion:** default read/search visibility and the federation feed
  subtract the secret vault namespace on every backend. Federation applies the
  subtraction unconditionally, so credential-class rows are never emitted even
  when a peer explicitly requests `namespace=vault`.
- **Version snapshots and DAG reads:** branch heads, version exports,
  commit-log/diff/checkout, referenced-memory allowlists, and cleanup-time
  snapshot suppression are exercised through the backend-neutral repositories.
  Postgres uses its trigger/GUC path; SQLite and enterprise backends expose the
  same repository contract without leaking trigger-only assumptions.
- **Migrations:** SQLite has a mirrored migration-file chain check; Oracle and
  Db2 carry native schema bootstraps for the same sidecar tables used by the
  parity tests; MySQL creates its native DDL in the backend opener.
- **Transactions:** commit, explicit rollback, exception rollback, webhook
  outbox atomicity, and rollback-with-memory are tested through
  `PersistenceBackend.transactional()` instead of backend-specific SQL.
- **Federation feed:** all backends implement the compound cursor, tombstone
  feed rows, vault exclusion, and `prefer_compressed=True` variant selection.
  The previous SQLite compressed-feed `NotImplementedError` and Oracle/Db2
  ignored-flag behavior are removed.

## Tests

- `tests/test_persistence_parity.py`
- `tests/test_federation_repository.py`
- `tests/test_federation_backend_parity_static.py`
