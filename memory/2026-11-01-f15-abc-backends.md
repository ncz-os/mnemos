# 2026-11-01 — F-15 round 2 delivery + real ABC gap

## What the validator was actually failing on
`/opt/graeae/hive_worker.py --verify-delivery` reported `remote_verified: false`.
Cause: local HEAD `d4ee77b1` (merge of origin/master) was **never pushed**;
remote branch sat at `b18aa604`, 17 commits behind, 0 ahead. The F-15 tests
themselves were already green (21 passed). Delivery-only failure.

## Real defect found while auditing the review findings
Most of the review findings were **stale** (written against an older diff) and
were disproved against source:
- `_require_federation_role` / `require_federation_backend` — defined/imported
  (`routes/federation.py:18-20,46`).
- `count_federated_rows_without_embedding_per_peer` — exists as ABC method
  (`base.py:2593`) + SQLite impl (`sqlite.py:6663`).
- `embed_declined` / `embed_absent` — initialized to 0 (`federation.py:956-957`).
- `_peer_sync_lock`, `_encode/_decode_feed_cursor`, `_parse_cursor_timestamp` —
  all present (`federation.py:73,142,149,158`).
- "fetch_parity_page must filter by peer" — **wrong**: `parity._replica_keyset`
  filters `peer_name` after each page by design.

BUT one was **real and critical**: the four F-15 abstract methods added to
`FederationRepository` were implemented **only in SQLite**. Postgres, Oracle,
MySQL, MariaDB and Db2 repositories could not be instantiated at all
(`TypeError: Can't instantiate abstract class ...`). SQLite-only tests could
never catch it. This is a direct ABC-first violation (AGENTS.md governing rule).

## Fix
Implemented `fetch_parity_page`, `fetch_source_export_keyset_page`,
`count_exportable_rows`, `redispatch_federation_journal` in
`postgres.py`, `oracle.py`, `mysql.py` (MySQL impl serves MariaDB; Db2 inherits
Oracle and its cursor layer rewrites `:name` binds + accepts SUBSTR/LENGTH/
FETCH FIRST natively). Added `test_every_backend_repository_implements_the_f15_surface`
and a parametrized per-backend test — 6/6 fail without the fix.

## Env notes
- `tests/test_knossos_phase1.py` fails to collect on **base too**
  (`'Server' object has no attribute 'list_tools'`) — pre-existing
  dependency drift, unrelated.
- `postgres.py` is not ruff-format clean at baseline (drift at line 858+,
  1146+, my code is 6136+) — left alone per "no unrelated reformatting".
